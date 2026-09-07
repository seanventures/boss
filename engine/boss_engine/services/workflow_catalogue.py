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
    if approval or approver_role:
        step["approval"] = {}
        if approval:
            step["approval"]["mode"] = approval
        if approver_role:
            step["approval"]["approver_role"] = approver_role
    return step


#: `{"from_input": "directive"}` — read what the run was started with. A catalogue workflow
#: cannot hard-code the thing being built; "Standard Product Delivery" has to work for any
#: directive anyone gives it.
def _in(field: str) -> dict[str, str]:
    return {"from_input": field}


def _from(step_id: str, field: str) -> dict[str, str]:
    return {"from_step": step_id, "field": field}


def _build_inputs() -> dict[str, Any]:
    """What the build step reads from the run's input.

    Title and instructions are what the work IS. The other three are what the New Project
    wizard collects and every other entry point omits — a chosen repository, the criteria the
    operator wrote, the links to build against — so they are bound here and declared OPTIONAL
    on the capability. An absent optional input resolves to absent rather than failing the run,
    which is what makes one catalogue serve both the wizard and a one-line directive typed into
    a chat.
    """
    return {
        "title": _in("title"),
        "instructions": _in("directive"),
        "repository": _in("repository"),
        "acceptance_criteria": _in("acceptance_criteria"),
        "reference_links": _in("reference_links"),
    }


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
            _step("spec_review", "spec_review", approver_role="product_owner"),
            _step("build", "build", _build_inputs()),
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
            # AND PREVIEW AGAIN AFTER IT CHANGES CODE.
            #
            # PLRR writes code. Deploying straight after it would ship something nobody looked
            # at, which voids the gate the earlier preview exists to be. The second preview is
            # cheap — the app is already built — and it is what keeps "a person saw this" true.
            _step("preview_final", "preview",
                  {"run_id": _from("build", "code_task_run_id")}),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
            _step("notify", "notify", {"message": _in("title")}),
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
            _step("build", "build", _build_inputs()),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("notify", "notify", {"message": _in("title")}),
        ]),
    },
    {
        "key": "major_feature",
        "name": "Major Feature",
        "description": "A feature large enough to deserve a spec of its own and a person "
                       "signing off on it before anyone builds anything.",
        "objective": (
            "Specify a feature large enough to deserve its own spec, then build it and have a "
            "person sign off the way it looks and works. The PRD is reviewed by a human before "
            "any code and must be arguable on its merits, not just implementable."
        ),
        "work_class": "feature.major",
        "steps": _chain([
            _step("research", "research", {"seed": _in("directive")}),
            # THE PANEL ARGUES FROM THE RESEARCH. Without this binding the research step ran,
            # produced a cited dossier, and the panel that followed it debated the original
            # one-line directive as though none of it had happened.
            _step("meeting", "meeting", {"directive": _in("directive"),
                                         "research": _from("research", "dossier")}),
            _step("spec_review", "spec_review", approver_role="product_owner"),
            _step("build", "build", _build_inputs()),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
            _step("ux_review", "ux_review", approver_role="design_lead"),
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
        "objective": (
            "Something is broken in production now. The build has already run; this panel is the "
            "retrospective — what happened, why, and what stops it recurring. Do not re-specify "
            "the fix."
        ),
        "work_class": "bugfix",
        "steps": _chain([
            _step("build", "build", _build_inputs()),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("notify", "notify", {"message": _in("title")}),
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
            _step("meeting", "meeting", {"directive": _in("directive")}),
            _step("spec_review", "spec_review", approver_role="product_owner"),
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
            _step("spec_review", "spec_review", approver_role="tech_lead"),
            _step("build", "build", _build_inputs()),
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
            "sign it off, so the PRD must state the interaction, the states (empty, loading, "
            "error) and what 'good' means concretely enough for a reviewer to judge against."
        ),
        "work_class": "feature.major",
        "steps": _chain([
            _step("meeting", "meeting", {"directive": _in("directive")}),
            _step("build", "build", _build_inputs()),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
            _step("ux_review", "ux_review", approver_role="design_lead"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
        ]),
    },
    {
        "key": "security_hardening",
        "name": "Security Hardening",
        "description": "Threat-led work with a security sign-off that cannot be automated away.",
        "objective": (
            "Threat-led work ending in a security sign-off that cannot be automated away. The "
            "panel argues the threat model: what an attacker would try, what is exposed, and what "
            "evidence the reviewer will need."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("research", "research", {"seed": _in("directive")}),
            _step("build", "build", _build_inputs()),
            _step("review", "review", {"seats": ["security"]}),
            _step("security_review", "security_review", approver_role="security_lead"),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager"),
            _step("monitor", "monitor"),
        ]),
    },
    {
        "key": "data_migration",
        "name": "Data / Migration Change",
        "description": "Changes to data are the ones you cannot take back. Reviewed by someone "
                       "who owns the data, with the rollback proven before it ships.",
        "objective": (
            "Change data, which is the change you cannot take back. The PRD must state the "
            "rollback, how the migration is verified, and what happens to in-flight work — a "
            "person reviews it before anything runs."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("meeting", "meeting", {"directive": _in("directive")}),
            _step("spec_review", "spec_review", approver_role="data_owner"),
            _step("build", "build", _build_inputs()),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="data_owner"),
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
            _step("spec_review", "spec_review", approver_role="product_owner"),
            _step("build", "build", _build_inputs()),
            _step("security_review", "security_review", approver_role="security_lead"),
            _step("preview", "preview", {"run_id": _from("build", "code_task_run_id")}),
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
            _step("spec_review", "spec_review", approver_role="tech_lead"),
            _step("build", "build", _build_inputs()),
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
            _step("announce", "notify", {"message": _in("title")}, nxt=["grace"]),
            # The one control step in the catalogue: a real grace period, not a promise of one.
            {"id": "grace", "kind": "timer", "wait_seconds": 604800, "binding": {},
             "next": ["build"]},
            _step("build", "build", _build_inputs(), nxt=["deploy"]),
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  nxt=["document"], approver_role="release_manager"),
            _step("document", "document", {"title": _in("title")}),
        ],
    },
]


def catalogue_summary() -> list[dict[str, Any]]:
    """What a customer sees when choosing. Includes what the workflow will need from their
    project, so "you have not connected a repository" is said at the point of choosing."""
    out = []
    for entry in CATALOGUE:
        # THE OBJECTIVE TRAVELS WITH THE SPEC, so a step can read what the workflow it is
        # part of is FOR. Without it every panel in all twelve workflows convened the
        # same generic PRD meeting: Research-Led Discovery, whose own description says it
        # "ends in a decision and a document, not in code", asked for an implementation
        # plan exactly like Standard Product Delivery did.
        spec = {"entry_step": entry["steps"][0]["id"], "steps": entry["steps"],
                "objective": entry.get("objective") or "",
                "workflow_name": entry["name"]}
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
        gates = [
            s["id"] for s in effective["steps"]
            if (s.get("approval") or {}).get("mode") == "manual"
        ]
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
            "requires_project": sorted(project_requirements(spec)),
            "estimated_minutes": estimate_minutes(spec),
        })
    return out


def seed(connection: Connection, project_id: str, actor: str = "boss") -> dict[str, Any]:
    """Install the catalogue into a project. Idempotent.

    Re-running publishes a new version ONLY when the spec actually changed — the runtime keys
    versions by content hash, so an unchanged catalogue re-seeds to the same version rather
    than accumulating identical ones on every boot.
    """
    installed: list[dict[str, Any]] = []
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

        # THE OBJECTIVE TRAVELS WITH THE SPEC, so a step can read what the workflow it is
        # part of is FOR. Without it every panel in all twelve workflows convened the
        # same generic PRD meeting: Research-Led Discovery, whose own description says it
        # "ends in a decision and a document, not in code", asked for an implementation
        # plan exactly like Standard Product Delivery did.
        spec = {"entry_step": entry["steps"][0]["id"], "steps": entry["steps"],
                "objective": entry.get("objective") or "",
                "workflow_name": entry["name"]}
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
    return {"project_id": project_id, "installed": installed, "count": len(installed)}
