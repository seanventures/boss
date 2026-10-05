"""The delivery workflows BOSS ships.

Twelve, chosen to cover how software actually gets delivered rather than to fill a grid. A
customer picks one, applies it to a project or to a single feature, and the work walks the
steps. To change one they fork it — these stay pristine, which is what keeps "reset to the
standard" meaningful and keeps their diff against it visible.

Every step here is validated by the same code that validates a customer's own workflow. If a
capability's inputs change and a catalogue entry stops making sense, the seeding fails loudly
at startup rather than shipping twelve workflows that cannot run.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import workflows
from .workflow_capabilities import estimate_minutes, project_requirements

#: Which class of work a catalogue workflow is FOR. A customer applying "Fast Fix" to every
#: new project would be a mistake worth preventing at the point of choosing.
WORK_CLASSES = ("project.new", "feature.major", "feature.minor", "bugfix", "any")

#: The same five, in words an operator uses. `project.new` is a selector value — a key in a
#: unique index — and it leaked into a confirmation message shown beside a dropdown that says
#: "Research-Led Discovery": "New project.new work now uses 'research_led_discovery'." Two
#: identifiers and a dot, to describe a choice the operator had just made in plain English.
WORK_CLASS_LABELS = {
    "project.new": "new projects",
    "feature.major": "major features",
    "feature.minor": "small features",
    "bugfix": "bug fixes",
    "any": "any work without a more specific rule",
}


def work_class_label(work_class: str) -> str:
    """What this class of work is called out loud. Falls back to the key rather than to ''."""
    return WORK_CLASS_LABELS.get(work_class, work_class)


#: WHO DOES EACH KIND OF STEP, BY DEFAULT (agent-world spec §0.1 item 4). Every catalogue step
#: of these kinds names its agent, so a stock run is staffed — Juno convenes the panel, Helm
#: runs the build, Vera checks the preview, Beacon holds it to readiness and watches it — and
#: the run's chain shows who did what instead of "You" on every step.
#:
#: Only steps whose work is BOSS's own: naming an agent there changes who the work is credited
#: to and who answers for it, never what it may reach (a built-in needs no grant —
#: step_agents). Gates keep naming their approver in `approval.approver_role`; the deploy step
#: keeps running as the run's actor, since its stack plan records who asked. An operator who
#: clears a step's agent in the Studio gets Auto, exactly as before.
#:
#: A CATALOGUE DEFAULT IS SOFT. Each such step also carries `default_captain` — the same key —
#: which says "BOSS put this agent here, nobody chose it" (step_agents.is_default_captain). A
#: soft default never pins its agent: the agent can still be deleted (the catalogue is
#: re-staffed without it — agents.delete_agent), and a paused, retired or missing one hands the
#: step back to Auto instead of failing every stock run (step_agents.resolve). An operator who
#: picks a different agent in the Studio makes the marker stop matching, so their choice is a
#: real one and holds like any other.
DEFAULT_STEP_AGENTS: dict[str, str] = {
    "research": "pm",
    "meeting": "pm",
    "build": "engineering_captain",
    "retry_build": "engineering_captain",
    "preview": "qa",
    "review": "adversarial_reviewer",
    "plrr": "sre",
    "monitor": "sre",
    "document": "docs_writer",
}

#: WHO BUILDS IT DEPENDS ON WHAT IS BEING BUILT (§0.1 item 4: "build → Engineering Captain /
#: Backend or Frontend Engineer by app kind"). A template whose whole point is a screen is built
#: by the Frontend Engineer; one whose point is a service, data or an integration by the
#: Backend Engineer — so their skills, MCP servers and voice are the ones a stock run reaches.
#: A general-purpose template keeps the Engineering Captain, who coordinates a full-stack build.
BUILD_AGENT_BY_ENTRY: dict[str, str] = {
    "ux_certification": "frontend_engineer",
    "backend_service_setup": "backend_engineer",
    "data_migration": "backend_engineer",
    "third_party_integration": "backend_engineer",
    "deprecation_sunset": "backend_engineer",
}

#: Who stands in for a specialist a project does not have, before falling back to Auto.
DEFAULT_CAPTAIN_FALLBACK: dict[str, str] = {
    "frontend_engineer": "engineering_captain",
    "backend_engineer": "engineering_captain",
}

#: The capabilities BUILD_AGENT_BY_ENTRY re-staffs.
_BUILD_CAPABILITIES = frozenset({"build", "retry_build"})


def _step(
    step_id: str,
    capability: str,
    inputs: Optional[dict[str, Any]] = None,
    *,
    nxt: Optional[list[str]] = None,
    approval: Optional[str] = None,
    approver_role: Optional[str] = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": step_id,
        "kind": "capability",
        "binding": {"capability": capability, "inputs": inputs or {}},
        "next": nxt or [],
    }
    if capability in DEFAULT_STEP_AGENTS:
        _set_default_captain(step, DEFAULT_STEP_AGENTS[capability])
    if approval or approver_role:
        step["approval"] = {}
        if approval:
            step["approval"]["mode"] = approval
        if approver_role:
            step["approval"]["approver_role"] = approver_role
    return step


def _set_default_captain(step: dict[str, Any], role_key: Optional[str]) -> dict[str, Any]:
    """Name a SOFT default agent on a catalogue step (see DEFAULT_STEP_AGENTS), or clear it."""
    step.pop("captain", None)
    step.pop("default_captain", None)
    if role_key:
        step["captain"] = role_key
        step["default_captain"] = role_key
    return step


#: `{"from_input": "directive"}` — read what the run was started with. A catalogue workflow
#: cannot hard-code the thing being built; "Standard Product Delivery" has to work for any
#: directive anyone gives it.
def _in(field: str) -> dict[str, str]:
    return {"from_input": field}


def _from(step_id: str, field: str) -> dict[str, str]:
    return {"from_step": step_id, "field": field}


def _build_inputs(after_meeting: bool = False) -> dict[str, Any]:
    """What the build step reads from the run's input.

    Title and instructions are what the work IS. The other three are what the New Project
    wizard collects and every other entry point omits — a chosen repository, the criteria the
    operator wrote, the links to build against — so they are bound here and declared OPTIONAL
    on the capability. An absent optional input resolves to absent rather than failing the run,
    which is what makes one catalogue serve both the wizard and a one-line directive typed into
    a chat.

    THE PANEL'S PRD REACHES THE BUILDER. `after_meeting` binds it.

    This workflow's own objective says "the panel's PRD is the build brief: it is handed
    straight to an engineering agent". It was not. The build bound `instructions` to the
    original one-line directive and nothing else, so an interview-scorecard build received 745
    characters — the operator's sentence plus the deployability boilerplate — after 7m49s of
    research and a 22m48s panel whose UX role had specified a rubric, behavioural anchors and
    per-interviewer submission status. None of it was in the brief. The agent built exactly
    what it was asked for: a JSON API with no UI, which no model could have avoided, because
    nothing told it a screen was wanted.

    This is the same wiring bug as the one fixed one step earlier, recorded in this file at the
    meeting step: "the panel that followed it debated the original one-line directive as though
    none of it had happened". Research→meeting was wired then. Meeting→build was not.
    """
    inputs = {
        "title": _in("title"),
        "instructions": _in("directive"),
        "repository": _in("repository"),
        "acceptance_criteria": _in("acceptance_criteria"),
        "reference_links": _in("reference_links"),
    }
    if after_meeting:
        inputs["spec_markdown"] = _from("meeting", "prd_markdown")
    return inputs


def _notify_inputs() -> dict[str, Any]:
    """What a notify step reads: the run's name, and WHO TO TELL.

    `channel` comes from the run's `notify_channel` input — an email address or a Mattermost
    channel the operator types when starting. It was never bound, so every notify step in the
    catalogue had nowhere to post and recorded a green no-op (boss-1 map, 2026-10-02).
    """
    return {"message": _in("title"), "channel": _in("notify_channel")}


def _chain(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wire a straight sequence. Every catalogue workflow is a sequence with gates in it, and
    hand-writing `next` twelve times is twelve chances to mis-wire one."""
    for current, following in zip(steps, steps[1:]):
        current["next"] = [following["id"]]
    steps[-1]["next"] = []
    return steps


#: The catalogue. `key` is stable and is what a mapping points at.
# WHY THERE IS NO `review` OR `test` STEP IN ANY OF THESE.
#
# There are two loops. The OUTER one is these steps — the path a customer defines and can
# change. The INNER one is the governed coding pipeline BOSS runs inside `build`: implement,
# adversarial review, security check, mutation check, run the tests, open a pull request. The
# outer loop must not reach into the inner one; judging a build gate from out here is the outer
# loop doing the inner loop's job, and doing it worse.
#
# So `build` is one step covering build-review-test, and its outcome is the only question the
# outer loop needs answered: DID IT PRODUCE A PULL REQUEST? If the adversarial or security
# reviewer refuted the change and no PR was opened, the step fails — that is the inner loop
# reporting its verdict through its result, not the outer loop second-guessing it.
#
# A PR means there is something to look at, so `preview` follows automatically. `deploy` is the
# one thing a person still decides: not every app that builds should ship.
CATALOGUE: list[dict[str, Any]] = [
    {
        "key": "standard_product_delivery",
        "name": "Standard Product Delivery",
        "description": "Idea to shipped, watched software. The full path, with gates where "
                       "money and production are involved.",
        "objective": (
            "Ship working, watched software. The panel's PRD is the build brief: it is handed "
            "straight to an engineering agent, so it must be specific enough to implement and to "
            "test against."
        ),
        "work_class": "project.new",
        "steps": _chain([
            _step("research", "research", {"seed": _in("directive")}),
            # THE PANEL ARGUES FROM THE RESEARCH. Without this binding the research step ran,
            # produced a cited dossier, and the panel that followed it debated the original
            # one-line directive as though none of it had happened.
            _step("meeting", "meeting", {"directive": _in("directive"),
                                         "research": _from("research", "dossier")}),
            _step("spec_review", "spec_review", approver_role="pm"),
            _step("build", "build", _build_inputs(after_meeting=True)),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
            # READINESS BEFORE PRODUCTION, NOT AFTER IT.
            #
            # Preview answers "does it do the thing". It says nothing about whether anyone can
            # RUN the thing: whether a failing feature is visible, whether a bad release can be
            # undone, whether 3am has a first command. Companies hold that conversation after
            # launch, and the first on-call night is what discovers the app has no metrics and
            # no health check. The operations lens named those signals during the panel; this
            # step holds the built app against what that spec promised and closes the gaps in
            # code before production, rather than filing them as follow-ups nobody runs.
            _step("plrr", "plrr", {"run_id": _from("build", "code_task_run_id")}),
            # NO SECOND PREVIEW AFTER READINESS (removed 2026-10-03, the operator: "it's not
            # useful"). The earlier preview is the human look at what was built; readiness then
            # closes operability gaps (health checks, metrics, rollback notes), and the deploy
            # below ships the run readiness waited for — so the repair is what ships, and the
            # run report says exactly what readiness changed before anyone approves the deploy.
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("plrr", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
            _step("notify", "notify", _notify_inputs()),
        ]),
    },
    {
        "key": "minor_feature",
        "name": "Minor Feature",
        "description": "A small addition to something that already exists. Skips research; "
                       "still reviewed, still gated at the door to production.",
        "objective": (
            "Add one small thing to software that already exists, quickly. There is no research "
            "step and no spec review — the PRD goes straight to the build, so keep it tight and "
            "do not redesign the surrounding product."
        ),
        "work_class": "feature.minor",
        "steps": _chain([
            _step("meeting", "meeting", {"directive": _in("directive")}),
            _step("build", "build", _build_inputs(after_meeting=True)),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("notify", "notify", _notify_inputs()),
        ]),
    },
    {
        "key": "major_feature",
        "name": "Major Feature",
        # NO LONGER PROMISES A SIGN-OFF BEFORE THE BUILD. This said "a person signing off on it
        # before anyone builds anything"; the step that stood for that is the PRD approval,
        # which is automatic by default (BOSS_MEETING_PRD_AUTO_APPROVE). The sign-offs a person
        # really gives here are on the preview and on the release, so that is what it says.
        "description": "A feature large enough to deserve research and a spec of its own. A "
                       "person signs off how it looks and works, and again before it ships.",
        "objective": (
            "Specify a feature large enough to deserve its own spec, then build it. The PRD is "
            "the build brief and must be arguable on its merits, not just implementable. Once "
            "it is built, a person signs off the way it looks and works."
        ),
        "work_class": "feature.major",
        "steps": _chain([
            _step("research", "research", {"seed": _in("directive")}),
            # THE PANEL ARGUES FROM THE RESEARCH. Without this binding the research step ran,
            # produced a cited dossier, and the panel that followed it debated the original
            # one-line directive as though none of it had happened.
            _step("meeting", "meeting", {"directive": _in("directive"),
                                         "research": _from("research", "dossier")}),
            _step("spec_review", "spec_review", approver_role="pm"),
            _step("build", "build", _build_inputs(after_meeting=True)),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
            _step("ux_review", "ux_review", approver_role="design_critic"),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
        ]),
    },
    {
        "key": "fast_fix",
        "name": "Fast Fix / Hotfix",
        "description": "Something is broken now. No research, no panel — build, prove it, "
                       "have someone say yes, ship.",
        # NO PANEL HERE, SO THE OBJECTIVE NO LONGER TALKS TO ONE. It said "this panel is the
        # retrospective" in a workflow with no meeting step; the objective is shown to the
        # operator and handed to the steps, so it has to describe the steps that exist.
        "objective": (
            "Something is broken in production now. Fix exactly that: the smallest change that "
            "makes the failure stop, with a test that would have caught it. Do not redesign "
            "the surrounding code; a person approves the release."
        ),
        "work_class": "bugfix",
        "steps": _chain([
            _step("build", "build", _build_inputs()),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("notify", "notify", _notify_inputs()),
        ]),
    },
    {
        "key": "research_led_discovery",
        "name": "Research-Led Discovery",
        "description": "Find out whether it is worth building at all. Ends in a decision and a "
                       "document, not in code.",
        "objective": (
            "Decide whether this is worth building AT ALL. This run ends in a decision and a "
            "written record, not in code. The panel's job is a recommendation with the case for "
            "and against and what would change the answer — not an implementation plan."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("research", "research", {"seed": _in("directive"), "deep": True}),
            # THE PANEL ARGUES FROM THE RESEARCH HERE TOO. This meeting read the directive and
            # nothing else — the wiring bug already fixed in Standard Product Delivery — so the
            # one workflow whose whole output is a researched recommendation paid for deep
            # research and then debated without it.
            _step("meeting", "meeting", {"directive": _in("directive"),
                                         "research": _from("research", "dossier")}),
            _step("spec_review", "spec_review", approver_role="pm"),
            _step("document", "document", {"title": _in("title")}),
        ]),
    },
    {
        "key": "backend_service_setup",
        "name": "Backend Service Setup",
        "description": "Stand up a new service: scaffold, endpoints, tests, deploy, watch.",
        "objective": (
            "Stand up a new service: scaffold, endpoints, tests, deployment and monitoring. The "
            "PRD is the build brief and must name the endpoints, the data it owns and how it is "
            "watched."
        ),
        "work_class": "project.new",
        "steps": _chain([
            _step("meeting", "meeting", {"directive": _in("directive")}),
            _step("spec_review", "spec_review", approver_role="cto"),
            _step("build", "build", _build_inputs(after_meeting=True)),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
            _step("document", "document", {"title": _in("title")}),
        ]),
    },
    {
        "key": "ux_certification",
        "name": "Frontend / UX Certification",
        "description": "For work whose whole point is how it looks and feels. A person uses "
                       "the preview and signs off before it ships.",
        "objective": (
            "The point of this work is how it looks and feels. A person will use the preview and "
            "sign it off. The PRD must therefore state the interaction, the states (empty, "
            "loading, error) and what 'good' means concretely enough for a reviewer to judge "
            "against."
        ),
        "work_class": "feature.major",
        "steps": _chain([
            _step("meeting", "meeting", {"directive": _in("directive")}),
            _step("build", "build", _build_inputs(after_meeting=True)),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
            _step("ux_review", "ux_review", approver_role="design_critic"),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
        ]),
    },
    {
        "key": "security_hardening",
        "name": "Security Hardening",
        "description": "Threat-led work with a security sign-off that cannot be automated away.",
        # THERE IS NO PANEL, AND THE RESEARCH NOW REACHES THE BUILD. The objective said "the
        # panel argues the threat model" in a workflow with no meeting step, and the research it
        # paid for was handed to nothing: the build read only the directive. The threat research
        # is the build's brief now, which is the only reason to research first.
        "objective": (
            "Threat-led work ending in a security sign-off that cannot be automated away. The "
            "research names what an attacker would try and what is exposed; the build closes "
            "those, and the reviewer gets the evidence for each."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("research", "research", {"seed": _in("directive")}),
            _step("build", "build", {**_build_inputs(),
                                     "spec_markdown": _from("research", "dossier")}),
            _step("review", "review", {"seats": ["security"]}),
            _step("security_review", "security_review", approver_role="security_reviewer"),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
        ]),
    },
    {
        "key": "data_migration",
        "name": "Data / Migration Change",
        # THE PERSON IS AT THE RELEASE, AND THE TEXT SAYS SO. "A person reviews it before
        # anything runs" pointed at the PRD approval, which is automatic by default. The human
        # decision in this workflow is the deploy gate — the moment the migration would run.
        "description": "Changes to data are the ones you cannot take back. The rollback is "
                       "specified up front, and a person approves before the migration runs.",
        "objective": (
            "Change data, which is the change you cannot take back. The PRD must state the "
            "rollback, how the migration is verified, and what happens to in-flight work — a "
            "person approves the release before the migration runs."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("meeting", "meeting", {"directive": _in("directive")}),
            _step("spec_review", "spec_review", approver_role="cto"),
            _step("build", "build", _build_inputs(after_meeting=True)),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            # THE RELEASE MANAGER DECIDES THE RELEASE (§0.1 item 4), here as in every other
            # template: the CTO already approved the plan at spec_review.
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
            _step("document", "document", {"title": _in("title")}),
        ]),
    },
    {
        "key": "compliance_release",
        "name": "Compliance-Ready Release",
        "description": "Everything the standard path does, plus the evidence and the signature "
                       "an auditor will ask for.",
        "objective": (
            "Everything the standard path does, plus the evidence and the signature that make it "
            "auditable. The PRD must name the controls being satisfied and the evidence each "
            "produces."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("research", "research", {"seed": _in("directive")}),
            # THE PANEL ARGUES FROM THE RESEARCH. Without this binding the research step ran,
            # produced a cited dossier, and the panel that followed it debated the original
            # one-line directive as though none of it had happened.
            _step("meeting", "meeting", {"directive": _in("directive"),
                                         "research": _from("research", "dossier")}),
            _step("spec_review", "spec_review", approver_role="pm"),
            _step("build", "build", _build_inputs(after_meeting=True)),
            _step("security_review", "security_review", approver_role="security_reviewer"),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
            _step("document", "document", {"title": _in("title")}),
        ]),
    },
    {
        "key": "third_party_integration",
        "name": "Third-Party Integration",
        "description": "Bringing someone else's system in. Research first, because the "
                       "surprises are always in their API and not in your code.",
        "objective": (
            "Bring someone else's system in. Research went first because their API decides the "
            "design; the PRD must state the contract, the failure modes and what happens when "
            "they are down."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("research", "research", {"seed": _in("directive")}),
            # THE PANEL ARGUES FROM THE RESEARCH. Without this binding the research step ran,
            # produced a cited dossier, and the panel that followed it debated the original
            # one-line directive as though none of it had happened.
            _step("meeting", "meeting", {"directive": _in("directive"),
                                         "research": _from("research", "dossier")}),
            _step("spec_review", "spec_review", approver_role="cto"),
            _step("build", "build", _build_inputs(after_meeting=True)),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            _step("report", "report"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
        ]),
    },
    {
        "key": "deprecation_sunset",
        "name": "Deprecation / Sunset",
        "description": "Taking something away. The grace period is a step, so nobody removes "
                       "a thing the day after announcing it.",
        "objective": (
            "Take something away without stranding anyone. The panel must define the grace "
            "period, who is told and when, and what replaces it — the notification is a step, not "
            "an afterthought."
        ),
        "work_class": "any",
        "steps": [
            _step("meeting", "meeting", {"directive": _in("directive")}, nxt=["announce"]),
            # THE ANNOUNCEMENT IS THE POINT, SO ITS CHANNEL IS REQUIRED. As a no-op `notify` it
            # told nobody and the run then waited seven days for objections nobody could raise.
            # `notify_channel` is a required input of this workflow (see its `inputs`), so a run
            # cannot start without somewhere to announce, and the preflight checks it can send.
            _step("announce", "notify", _notify_inputs(), nxt=["grace"]),
            # The one control step in the catalogue: a real grace period, not a promise of one.
            {"id": "grace", "kind": "timer", "wait_seconds": 604800, "binding": {},
             "next": ["build"]},
            _step("build", "build", _build_inputs(after_meeting=True), nxt=["report"]),
            # THE NUMBERS, ONCE EVERYTHING THAT DOES WORK HAS DONE IT.
            #
            # Last, before the deploy gate — deliberately after readiness and its preview, so
            # the report can say what PLRR actually changed:
            #
            #     "I want to see the summary of what plr did in the workflow pipeline"
            #
            # Before the gate rather than after it, because deploy is the one step a person
            # decides and many runs stop there for good. A run that never ships still has a
            # report. And a run that stops even earlier is not left without one: the same
            # report is assembled on demand for any run, at any point, from the same evidence.
            # HAND-WIRED, because this workflow is. Every other catalogue entry goes through
            # `_chain`, which derives `next` from list order — so a step inserted into the list
            # is wired for free. Here the edges are written out, and a step dropped into the
            # list without them is simply unreachable: present in the plan, never run. The
            # validator caught it; without it the step would have shipped silently dead, which
            # is exactly how `plrr` spent its whole life.
            _step("report", "report", nxt=["deploy"]),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  nxt=["document"], approver_role="release_manager"),
            _step("document", "document", {"title": _in("title")}),
        ],
    },
]



def _staff_builds_by_app_kind() -> None:
    """Apply BUILD_AGENT_BY_ENTRY to the catalogue's build steps, once, at import."""
    for entry in CATALOGUE:
        builder = BUILD_AGENT_BY_ENTRY.get(entry["key"])
        for step in entry["steps"] if builder else ():
            if (step.get("binding") or {}).get("capability") in _BUILD_CAPABILITIES:
                _set_default_captain(step, builder)


_staff_builds_by_app_kind()


# ── What Start asks for ────────────────────────────────────────────────────────────────────────
#
# WHY EVERY TEMPLATE DECLARES ITS INPUTS NOW. No catalogue entry had an input_schema, so the
# console had nothing to draw and "Start" fell back to `sample: true`: the run was on a random
# demo idea rather than the operator's work, and spent research, a panel, a build and a pull
# request in THEIR repository on it. For the six `any`-class templates the sample was a new
# product brief ("Build a meeting cost meter") handed to a data migration or a deprecation
# (boss-1 map, 2026-10-02). Start now opens a form for the operator's own brief, and a sample
# is an explicit "try an example" that fits the template.

#: The fields a catalogue step reads from the run, described once. `directive` is worded per
#: template in BRIEFS, because "what should it do" and "what is broken" are different questions.
_FIELD_TEXT: dict[str, dict[str, Any]] = {
    "title": {"type": "string", "title": "Name",
              "description": "A short name for this piece of work. It names the run, the app "
                             "it builds and the written record.",
              "examples": ["Shift swap board"]},
    "repository": {"type": "string", "title": "Repository (optional)",
                   "description": "owner/name of the GitHub repository to build in. Leave blank "
                                  "for this project's primary repository."},
    "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                            "title": "How you will know it works (optional)",
                            "description": "Each line becomes a check the build is held to. "
                                           "Leave blank and the panel proposes them."},
    "reference_links": {"type": "array", "items": {"type": "string", "format": "uri"},
                        "title": "Links to read first (optional)",
                        "description": "Docs, designs or tickets the builder should read."},
    "notify_channel": {"type": "string", "title": "Who to tell",
                       "description": "Where the outcome is posted: email:someone@example.com or "
                                      "mattermost:<channel id>. Leave blank and the notify step "
                                      "is skipped and says so.",
                       "examples": ["email:team@example.com"]},
}

#: Per template: how its brief is asked for, an example that fits its purpose (the form's
#: placeholder and the explicit "try an example"), and any input required beyond what its steps
#: demand. Every CATALOGUE key must be here — `test_every_template_asks_for_its_own_brief`.
BRIEFS: dict[str, dict[str, Any]] = {
    "standard_product_delivery": {
        "directive": ("What should it do?",
                      "Who it is for and what a person can do with it, in two or three "
                      "sentences. The panel argues from this text and its PRD is the build "
                      "brief, so a thin brief produces a thin product."),
        "example": {"title": "Shift swap board",
                    "directive": "A shift-swap board where staff offer a shift they cannot "
                                 "work, colleagues claim it, and the manager approves the "
                                 "swap. Used on a phone, mid-shift."},
    },
    "minor_feature": {
        "directive": ("What should change, and in which app?",
                      "One small addition to an app that already exists. Name the app and the "
                      "change; this path has no research and no spec review."),
        "example": {"title": "Export swaps as CSV",
                    "directive": "In the existing Shift swap board: add a 'Download CSV' button "
                                 "to the approvals page that exports this month's approved "
                                 "swaps."},
    },
    "major_feature": {
        "directive": ("What is the feature?",
                      "The feature, who needs it and why. It is researched and specified "
                      "before it is built, and a person signs off the preview and the "
                      "release."),
        "example": {"title": "Recurring shift swaps",
                    "directive": "Let staff offer a recurring shift (every Tuesday for six "
                                 "weeks) in one action, with the manager approving the series "
                                 "or individual dates."},
    },
    "fast_fix": {
        "directive": ("What is broken?",
                      "What fails, where, and how to see it fail. The build makes the smallest "
                      "change that stops it, with a test that would have caught it."),
        "example": {"title": "Approvals page 500",
                    "directive": "In the Shift swap board, the approvals page returns HTTP 500 "
                                 "when a swap has no claimant. It should list the swap as "
                                 "unclaimed instead."},
    },
    "research_led_discovery": {
        "directive": ("What do you want to find out?",
                      "The question, and the decision it feeds. This ends in a written "
                      "recommendation, not in code."),
        "example": {"title": "Is a shift-swap app worth building?",
                    "directive": "Do hourly-staff teams of 20-200 people already have a good way "
                                 "to swap shifts, and would a manager pay for one that "
                                 "enforces approval rules?"},
    },
    "backend_service_setup": {
        "directive": ("What service should exist?",
                      "What it serves, the data it owns and who calls it. The PRD names the "
                      "endpoints and how it is watched."),
        "example": {"title": "Rota API",
                    "directive": "A small HTTP service that stores weekly rotas and answers "
                                 "'who is on shift at time T' for other apps, with a health "
                                 "check and request metrics."},
    },
    "ux_certification": {
        "directive": ("What is the experience?",
                      "The interaction, its states (empty, loading, error) and what 'good' "
                      "means, concretely enough for a reviewer to judge the preview against."),
        "example": {"title": "One-tap shift claim",
                    "directive": "Make claiming an offered shift one tap on a phone, with a "
                                 "clear confirmation and an undo for ten seconds."},
    },
    "security_hardening": {
        "directive": ("What needs hardening?",
                      "The system and the threat you are worried about. Research names what an "
                      "attacker would try; the build closes it and a person signs off the "
                      "security posture."),
        "example": {"title": "Harden swap approvals",
                    "directive": "Make sure only a shift's own manager can approve its swap: "
                                 "check for IDOR on the approve endpoint and for CSRF on the "
                                 "approvals form."},
    },
    "data_migration": {
        "directive": ("What data changes?",
                      "The change, the rollback, and what happens to work in flight. A person "
                      "approves before the migration runs."),
        "example": {"title": "Split shift times by timezone",
                    "directive": "Store shift start and end as UTC plus the site's timezone "
                                 "instead of local time, migrating existing rows, with a "
                                 "rollback that restores the old columns."},
    },
    "compliance_release": {
        "directive": ("What is being released, under which controls?",
                      "The change and the controls it must satisfy. Each control's evidence is "
                      "produced and a person signs off security and the release."),
        "example": {"title": "Audit log for approvals",
                    "directive": "Record every swap approval and rejection in an append-only "
                                 "audit log with who, when and why, retained for a year."},
    },
    "third_party_integration": {
        "directive": ("Which system are you bringing in, and for what?",
                      "The other system, what you need from it, and what should happen when "
                      "it is down. Research reads their API first."),
        "example": {"title": "Slack swap alerts",
                    "directive": "Post a message to the team's Slack channel when a shift is "
                                 "offered, and update it when the swap is claimed or approved."},
    },
    "deprecation_sunset": {
        "directive": ("What is being taken away?",
                      "What goes, what replaces it, and who must be told. The announcement is "
                      "sent first and the removal waits a week."),
        "example": {"title": "Retire the legacy swap form",
                    "directive": "Remove the old paper-style swap request form now that the "
                                 "board replaces it; redirect its URL to the board."},
        # THE ANNOUNCEMENT IS THE POINT. A sunset nobody was told about is the outcome this
        # workflow exists to prevent, so it cannot start without somewhere to announce.
        "required": ("notify_channel",),
    },
}


def _fields_read(steps: list[dict[str, Any]]) -> tuple[list[str], set[str]]:
    """Every run input the steps read, in first-read order, and the ones a step REQUIRES."""
    from .workflow_capabilities import CAPABILITIES

    order: list[str] = []
    required: set[str] = set()
    for step in steps:
        binding = step.get("binding") or {}
        capability = CAPABILITIES.get(str(binding.get("capability") or ""))
        for name, value in (binding.get("inputs") or {}).items():
            if isinstance(value, dict) and "from_input" in value:
                field = str(value["from_input"])
                if field not in order:
                    order.append(field)
                if capability and name in capability.required_inputs:
                    required.add(field)
    return order, required


def input_schema_for(entry: dict[str, Any]) -> dict[str, Any]:
    """The JSON Schema Start draws its form from: every field a step reads, required where a
    step cannot run without it, worded for this template."""
    brief = BRIEFS[entry["key"]]
    order, required = _fields_read(entry["steps"])
    required |= set(brief.get("required") or ())
    title, help_text = brief["directive"]
    properties: dict[str, Any] = {}
    for field in order:
        if field == "directive":
            properties[field] = {"type": "string", "format": "textarea", "title": title,
                                 "description": help_text,
                                 "examples": [brief["example"]["directive"]]}
        else:
            text_for = dict(_FIELD_TEXT[field])
            if field in required and text_for["title"].endswith(" (optional)"):
                text_for["title"] = text_for["title"][: -len(" (optional)")]
            properties[field] = text_for
    return {"type": "object",
            "required": [f for f in order if f in required],
            "properties": properties}


def entry_spec(entry: dict[str, Any]) -> dict[str, Any]:
    """The spec a catalogue entry publishes — one construction for seeding and for the summary,
    so what a card describes is what a project runs."""
    # THE OBJECTIVE TRAVELS WITH THE SPEC, so a step can read what the workflow it is
    # part of is FOR. Without it every panel in all twelve workflows convened the
    # same generic PRD meeting: Research-Led Discovery, whose own description says it
    # "ends in a decision and a document, not in code", asked for an implementation
    # plan exactly like Standard Product Delivery did.
    return {"entry_step": entry["steps"][0]["id"], "steps": entry["steps"],
            "objective": entry.get("objective") or "",
            "workflow_name": entry["name"],
            "input_schema": input_schema_for(entry)}


def example_for(workflow_key: str) -> Optional[dict[str, Any]]:
    """The template's own example brief, for an explicit "try an example"."""
    brief = BRIEFS.get(str(workflow_key or ""))
    return dict(brief["example"]) if brief else None


def catalogue_summary(prd_auto_approve: Optional[bool] = None) -> list[dict[str, Any]]:
    """What a customer sees when choosing. Includes what the workflow will need from their
    project, so "you have not connected a repository" is said at the point of choosing.

    `gates` / `automatic_approvals` follow the PRD auto-approve setting (prd_auto_approved),
    the same rule GET /v1/delivery-workflows/asks and the runtime use."""
    from .workflow_capabilities import CAPABILITIES, prd_auto_approved, stops_for_a_person

    if prd_auto_approve is None:
        prd_auto_approve = prd_auto_approved()
    from .workflow_readiness import need_for, needs_of

    out = []
    for entry in CATALOGUE:
        spec = entry_spec(entry)
        # VALIDATE FIRST, then read the gates off the result.
        #
        # Effective approval is computed during validation — a deploy step written without an
        # approval block still comes out manual, because the risk floor says so. Counting gates
        # from the raw catalogue entry reported "Minor Feature: 0 gates" for a workflow that
        # stops at the door to production, which is the opposite of the truth and exactly the
        # number a customer would choose on.
        effective = workflows.validate_workflow_spec(
            {"entry_step": spec["entry_step"],
             "steps": [dict(step) for step in entry["steps"]]}
        )
        manual = [
            s for s in effective["steps"]
            if (s.get("approval") or {}).get("mode") == "manual"
        ]
        # A GATE IS WHERE A PERSON IS ASKED. The PRD approval is manual by the risk floor and
        # answered by the panel's own decision — automatically by default — so it is listed
        # apart rather than as "you approve: spec_review".
        def _capability(step: dict[str, Any]) -> str:
            return str((step.get("binding") or {}).get("capability") or "")

        gates = [s["id"] for s in manual
                 if _capability(s) not in CAPABILITIES
                 or stops_for_a_person(_capability(s), (s.get("approval") or {}).get("mode"),
                                       prd_auto_approve=prd_auto_approve)]
        automatic = [s["id"] for s in manual if s["id"] not in gates]
        needs = needs_of(spec)
        out.append({
            "key": entry["key"],
            "name": entry["name"],
            "description": entry["description"],
            "work_class": entry["work_class"],
            "step_count": len(entry["steps"]),
            "steps": [
                {"id": s["id"],
                 "capability": (s.get("binding") or {}).get("capability") or s["kind"]}
                for s in entry["steps"]
            ],
            "gates": gates,
            "automatic_approvals": automatic,
            "requires_project": sorted(project_requirements(spec)),
            # EVERYTHING it needs from a project, including a model and the agents its gates
            # name — the same keys the start preflight checks, with their labels.
            "needs": [{"key": key, "label": need_for(key).label} for key in needs],
            "estimated_minutes": estimate_minutes(spec),
            # WHAT IT SPENDS, not only how long it takes: PLRR is a second build pass.
            "cost_notes": [
                {"step": s["id"], "note": CAPABILITIES[_capability(s)].cost_note}
                for s in entry["steps"]
                if _capability(s) in CAPABILITIES and CAPABILITIES[_capability(s)].cost_note
            ],
            "input_schema": spec["input_schema"],
            "example": example_for(entry["key"]),
        })
    return out


def seed(connection: Connection, project_id: str, actor: str = "boss") -> dict[str, Any]:
    """Install the catalogue into a project. Idempotent.

    Re-running publishes a new version ONLY when the spec actually changed — the runtime keys
    versions by content hash, so an unchanged catalogue re-seeds to the same version rather
    than accumulating identical ones on every boot.
    """
    installed: list[dict[str, Any]] = []
    # THE AGENTS THIS PROJECT ACTUALLY HAS, not the bundled roster: a step naming an agent
    # with no roles row is refused at start ("there is no agent 'sre' on this project"), so a
    # default agent is named only where it exists.
    present = frozenset(str(k) for k in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": project_id},
    ).scalars().all())
    for entry in CATALOGUE:
        existing = connection.execute(
            text(
                "SELECT id FROM workflow_definitions "
                "WHERE project_id=:p AND workflow_key=:k"
            ),
            {"p": project_id, "k": entry["key"]},
        ).mappings().first()

        if existing:
            definition_id = existing["id"]
        else:
            definition = workflows.create_definition(
                connection,
                project_id=project_id,
                workflow_key=entry["key"],
                name=entry["name"],
                description=entry["description"],
                actor=actor,
                namespace=workflows.PRODUCT_NAMESPACE,
                is_catalogue=True,
            )
            definition_id = definition["id"]

        spec = entry_spec(_staffed_for(entry, present))
        try:
            version = workflows.publish_version(
                connection, project_id=project_id, definition_id=definition_id,
                spec=spec, actor=actor,
            )
            installed.append({"key": entry["key"], "version_id": version["id"],
                              "version": version["version"]})
        except workflows.WorkflowConflictError:
            # Same content hash: already published. Nothing to do, and not an error.
            installed.append({"key": entry["key"], "version_id": None, "version": None})
    mapped = _seed_default_mappings(connection, project_id, actor=actor)
    return {"project_id": project_id, "installed": installed, "count": len(installed),
            "mapped": mapped}


def _staffed_for(entry: dict[str, Any], present: frozenset[str]) -> dict[str, Any]:
    """The entry with every default agent this project HAS; the rest left to Auto.

    A project whose operator deleted a shipped agent, or that never installed the pack that
    ships it, would otherwise be published a step naming an agent it does not have — refused
    at publish for a deleted one (stopping the whole catalogue from seeding) and refused at
    start for a missing one. Such a step is published unnamed (Auto) there, and only there;
    the next seed after the agent appears names it again.
    """
    steps = []
    for step in entry["steps"]:
        default = step.get("default_captain")
        if default and step.get("captain") == default and default not in present:
            stand_in = DEFAULT_CAPTAIN_FALLBACK.get(default)
            step = _set_default_captain(dict(step),
                                        stand_in if stand_in in present else None)
        elif step.get("captain") and step["captain"] not in present:
            step = {k: v for k, v in step.items() if k != "captain"}
        steps.append(step)
    return {**entry, "steps": steps}


def restaff_without(connection: Connection, project_id: str, role_key: str, *,
                    actor: str) -> list[dict[str, Any]]:
    """Take one agent off every catalogue step that names it only as a SOFT default.

    Called when that agent is deleted (agents.delete_agent). For each catalogue workflow whose
    ACTIVE version names it as a default, the same spec — operator edits and all — is published
    again with that step handed to its stand-in or to Auto, so the Studio shows who runs it now
    and a later edit does not trip over a name the project no longer has. A step an operator
    named the agent on themselves is not touched here (and blocks the delete instead).
    """
    from .step_agents import is_default_captain

    key = str(role_key or "").strip().lower()
    present = frozenset(str(k) for k in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p AND role_key <> :k"),
        {"p": project_id, "k": key},
    ).scalars().all())
    rows = connection.execute(
        text(
            """
            SELECT d.id AS definition_id, d.workflow_key, v.spec
              FROM workflow_definitions d
              JOIN workflow_versions v ON v.id = d.active_version_id
             WHERE d.project_id = :p
            """
        ),
        {"p": project_id},
    ).mappings().all()
    republished: list[dict[str, Any]] = []
    for row in rows:
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        steps = spec.get("steps") or []
        if not any(isinstance(s, dict) and is_default_captain(s)
                   and str(s.get("captain")).lower() == key for s in steps):
            continue
        staffed = _staffed_for({"steps": [dict(s) for s in steps]}, present)
        try:
            version = workflows.publish_version(
                connection, project_id=project_id, definition_id=row["definition_id"],
                spec={**spec, "steps": staffed["steps"]}, actor=actor)
        except workflows.WorkflowConflictError:
            continue
        republished.append({"workflow_key": row["workflow_key"],
                            "version": version.get("version")})
    return republished


#: EXACTLY THE ONE THE CONSOLE ALREADY CALLS THE DEFAULT, and nothing else.
#:
#: "Installing twelve workflows is not the same as choosing one, and a project that silently
#: acquired a process nobody picked would be worse than a project with none." That rule is
#: right and it stands: bugfix, feature.major and feature.minor stay unmapped until somebody
#: chooses, and an unmapped class still resolves to nothing rather than a guess.
#:
#: The exception is `project.new`, because the composer's workflow picker already presents
#: "Standard (default)" as the selected option. Shipping a control that says a default is in
#: force while the router has none is the inconsistency that made the core feature fail on a
#: clean install: pressing Build answered 409 about a mapping the operator had been shown as
#: already chosen. Either the label was a lie or the mapping should exist; this makes it true.
_DEFAULT_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("project.new", "standard_product_delivery"),
)

#: Below anything a person sets. Priority is part of the mapping's unique slot AND of the
#: resolver's score, so seeding at the same 100 the console writes would both collide with an
#: explicit choice and compete with it. A default must lose to a decision.
_DEFAULT_MAPPING_PRIORITY = 10


def _repoint_default(
    connection: Connection, mapping_id: str, project_id: str, workflow_key: str,
) -> bool:
    """Move BOSS's own default mapping onto the newest published version of its workflow.

    Returns True when it moved. An operator's mapping is never touched: it is identified by
    NOT being at the seeded priority, or by pointing at a workflow other than this catalogue
    entry. Both are decisions, and a decision outranks a default.
    """
    row = connection.execute(
        text("SELECT m.priority, m.workflow_version_id, d.workflow_key, d.is_catalogue "
             "  FROM workflow_mappings m "
             "  JOIN workflow_versions v ON v.id = m.workflow_version_id "
             "  JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
             " WHERE m.id = :m"),
        {"m": mapping_id},
    ).mappings().first()
    if not row:
        return False
    if (row["priority"] != _DEFAULT_MAPPING_PRIORITY
            or row["workflow_key"] != workflow_key
            or not row["is_catalogue"]):
        return False                       # the operator's, not ours

    newest = connection.execute(
        text("SELECT v.id FROM workflow_versions v "
             "  JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
             " WHERE v.project_id = :p AND d.workflow_key = :k AND v.status = 'published' "
             " ORDER BY v.created_at DESC LIMIT 1"),
        {"p": project_id, "k": workflow_key},
    ).first()
    if not newest or newest[0] == row["workflow_version_id"]:
        return False                       # already current

    # Repointing changes which version work starts: not while an agent delete is deciding.
    workflows.hold_workflow_reach(connection, project_id)
    connection.execute(
        text("UPDATE workflow_mappings SET workflow_version_id = :v, updated_at = now() "
             " WHERE id = :m"),
        {"v": newest[0], "m": mapping_id},
    )
    return True


def _seed_default_mappings(
    connection: Connection, project_id: str, *, actor: str = "boss",
) -> list[dict[str, Any]]:
    """Route each class of work to a workflow, unless somebody already has.

    A published workflow that nothing points at is not reachable: pressing Build answered
    "No approved workflow mapping matched this class of work" on an install whose registry
    had just been filled. Installing and routing are one act for a default; choosing
    differently is the operator's, and this never overwrites a slot they own.
    """
    seeded: list[dict[str, Any]] = []
    for work_class, key in _DEFAULT_MAPPINGS:
        existing = connection.execute(
            text("SELECT id FROM workflow_mappings "
                 " WHERE project_id=:p AND selector_kind='work_class' "
                 "   AND selector_value=:sv AND enabled = true LIMIT 1"),
            {"p": project_id, "sv": work_class},
        ).first()
        if existing:
            # A DEFAULT THAT NEVER MOVES PINS THE PROJECT TO THE DAY IT WAS INSTALLED.
            #
            # This returned here unconditionally, so a mapping seeded on first boot stayed
            # pointed at version 1 for the life of the project — and workflow_mappings pins a
            # VERSION, not a definition. Every catalogue improvement after that first boot was
            # published, became the definition's active version, and was never reached by a
            # single run. The PRD-to-builder binding was inert for exactly this reason: v2 was
            # published and active, and `project.new` still resolved to v1.
            #
            # The rule this protects is still right — never overwrite a slot the operator owns.
            # So only BOSS's OWN default moves: the seeded priority, pointing at a version of
            # this same catalogue workflow. Anything else is somebody's decision and is left
            # exactly where it is.
            _repoint_default(connection, existing[0], project_id, key)
            continue
        version = connection.execute(
            text("SELECT v.id FROM workflow_versions v "
                 "  JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
                 " WHERE v.project_id=:p AND d.workflow_key=:k AND v.status='published' "
                 " ORDER BY v.created_at DESC LIMIT 1"),
            {"p": project_id, "k": key},
        ).first()
        if not version:
            continue                       # not installed here; nothing to point at
        try:
            workflows.create_mapping(
                connection, project_id=project_id, workflow_version_id=version[0],
                selector_kind="work_class", selector_value=work_class,
                scope_type="project", scope_id=project_id,
                priority=_DEFAULT_MAPPING_PRIORITY, actor=actor)
            seeded.append({"work_class": work_class, "workflow_key": key})
        except Exception:  # noqa: BLE001 — a mapping that will not seed must not stop the rest
            continue
    return seeded
