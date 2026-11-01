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

from . import dq_catalogue, two_pass, workflows
from .workflow_capabilities import CAPABILITIES, estimate_minutes, project_requirements

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
    # The builder starts the app it built: preview:manage is on the Builder Passport, not on
    # the QA agent's (Reviewer), whose every preview was refused and skipped (2026-10-13 pre-check).
    "preview": "engineering_captain",
    "review": "adversarial_reviewer",
    "plrr": "sre",
    "more_tests": "qa",
    "monitor": "sre",
    # SET UP MONITORING, before the release (monitoring_pack): Beacon writes the pack.
    "monitoring_pack": "sre",
    "document": "docs_writer",
    "investigate_incident": "sre",
    "verify_recovery": "sre",
    "incident_report": "sre",
    # THE SECURITY REVIEW: the security reviewer scans, re-reviews and reports; the PM convenes
    # the panel exactly as for any panel (the security lens chairs its seats); the engineering
    # captain builds the fix like any build.
    "security_scan": "security_reviewer",
    "security_panel": "pm",
    "security_fix": "engineering_captain",
    "security_rereview": "security_reviewer",
    "security_report": "security_reviewer",
    # THE RULES SCAN: the architect reads what the system enforces (rule_scan.DEFAULT_ROLE).
    "scan_business_rules": "chief_architect",
    # THE REGULATORY TRACE (regulatory_trace.DEFAULT_ROLE): the architect traces a document's
    # clauses to the rules and code that implement them.
    "trace_regulation": "chief_architect",
    # THE RULEBOOK EXTRACTION (rulebook_extract.ROLE): the Rulebook Analyst reads the rulebook.
    "extract_rulebook_rules": "rulebook_analyst",
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
_BUILD_CAPABILITIES = frozenset({"build", "retry_build", "security_fix"})


def _step(
    step_id: str,
    capability: str,
    inputs: Optional[dict[str, Any]] = None,
    *,
    nxt: Optional[list[str]] = None,
    approval: Optional[str] = None,
    approver_role: Optional[str] = None,
    follows_policy: bool = False,
) -> dict[str, Any]:
    """One catalogue step. `follows_policy` (a Deploy step only): the environment's deploy
    policy may answer for it (deploy_policy). Without it a Deploy step PROMISES A PERSON, and
    every surface says so — only a workflow whose words promise nobody at the release opts in."""
    step: dict[str, Any] = {
        "id": step_id,
        "kind": "capability",
        "binding": {"capability": capability, "inputs": inputs or {}},
        "next": nxt or [],
    }
    default = default_agent_for(capability)
    if default:
        _set_default_captain(step, default)
    if approval or approver_role or follows_policy:
        step["approval"] = {}
        if approval:
            step["approval"]["mode"] = approval
        if approver_role:
            step["approval"]["approver_role"] = approver_role
        if follows_policy:
            if capability != "deploy":
                raise ValueError("only a Deploy step can follow the environment's policy")
            step["approval"]["follows_policy"] = True
    return step


def default_agent_for(capability: str) -> str:
    """The agent a catalogue step of this capability names by default: DEFAULT_STEP_AGENTS, or
    else THE ROLE AGENT WHOSE JUDGEMENT THE STEP IS (Capability.role_agent — the Compliance
    Analyst, the AML Analyst, ...).

    A role-agent step that named nobody ran as the person (step_agents.resolve: run_actor), so
    the consent fell back to the capability's captain and asked for "Security Reviewer and
    Documentation Writer" to start an AML investigation that Mesh, Sift and Scribe do. Naming the
    role agent makes the step, the run view and the consent say the same agent."""
    if capability in DEFAULT_STEP_AGENTS:
        return DEFAULT_STEP_AGENTS[capability]
    cap = CAPABILITIES.get(capability)
    return str(getattr(cap, "role_agent", "") or "")


def roles_needed(entry: dict[str, Any]) -> list[str]:
    """The role agents an entry's steps are the judgement of (Capability.role_agent), in step
    order. These are pack roles no core cast installs, so seeding the entry installs them."""
    out: list[str] = []
    for step in entry.get("steps") or []:
        cap = CAPABILITIES.get(str((step.get("binding") or {}).get("capability") or ""))
        role = str(getattr(cap, "role_agent", "") or "")
        if role and role not in out:
            out.append(role)
    return out


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
#: An AML gate's subject: the alert, its rule, the account and the customer by name — falling
#: back to less when intake could not say it all (workflows._gate_text skips a template with a
#: hole).
_AML_ALERT_SUBJECT = [
    "{steps.alert_intake.output.alert_id} · {steps.alert_intake.output.rule} on "
    "{steps.alert_intake.output.account_id} ({steps.alert_intake.output.customer_node_id|name})",
    "{steps.alert_intake.output.alert_id} · {steps.alert_intake.output.rule} on "
    "{steps.alert_intake.output.account_id}",
    "{input.alert_id}",
]


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


#: INCREMENTAL BUILD's most increments. A bounded sequence expanded here, not a loop engine:
#: increment k > 1 starts with a condition that asks the approved plan whether it HAS an
#: increment k, and goes straight to the report when it does not.
MAX_INCREMENTS = 4


def increment_brief(k: int) -> str:
    """What the build of increment k is told, on top of the run's brief (the runner adds it
    for a build step with `increment`)."""
    if k == 1:
        return ("INCREMENT 1 of the plan at the end of the PRD: build ONLY what '## Increment 1' "
                "lists — the core of the brief, working end to end, so it can go live first. "
                "Later increments add the rest on this branch; do not build them now.")
    return (f"INCREMENT {k} of the plan at the end of the PRD: build ONLY what '## Increment "
            f"{k}' lists, on top of the increments already on this branch. Keep everything "
            "they built working; do not start later increments.")


def _increments(count: int) -> list[dict[str, Any]]:
    """Plan → approve → for each increment: build (pass 1) → deploy; the two-pass rule adds
    each increment's preview, More tests and Deploy again (two_pass). Every step of increment k
    is titled "Increment k · …" and its id starts `inc{k}_`, so a run shows it as one group.

    ONE BRANCH, ONE PULL REQUEST, A COMMIT SET PER INCREMENT. Increment k > 1 builds with
    `extend` — on this run's branch, from the newest finished pass — so the increments stack.
    Separate PRs would be stacked PRs, none reviewable or mergeable without the one before it;
    one PR whose commits arrive increment by increment, each previewed and deployed, keeps the
    review linear and every increment visible as it lands."""
    steps: list[dict[str, Any]] = [
        {**_step("meeting", "meeting", {"directive": _in("directive")}, nxt=["spec_review"]),
         "title": "Plan the increments"},
        {**_step("spec_review", "spec_review", nxt=["inc1_build"], approver_role="pm"),
         "title": "Approve the plan"},
    ]
    for k in range(1, count + 1):
        build_inputs = {**_build_inputs(after_meeting=True), "increment": k,
                        **({"extend": True} if k > 1 else {})}
        after_deploy = f"inc{k + 1}_planned" if k < count else "report"
        if k > 1:
            steps.append({
                "id": f"inc{k}_planned", "kind": "condition",
                "title": f"Increment {k} · In the plan?",
                "expression": {"path": "steps.meeting.output.prd_markdown", "op": "contains",
                               "value": f"## Increment {k}"},
                "on_true": f"inc{k}_build", "on_false": f"inc{k}_not_planned", "next": [],
            })
            # ITS OWN "NO" STEP, not the report itself: the branch a condition does not take is
            # marked skipped, and the report is where every path ends.
            steps.append({"id": f"inc{k}_not_planned", "kind": "timer", "wait_seconds": 0,
                          "binding": {}, "title": f"Increment {k} · Not in the plan",
                          "next": ["report"]})
        steps.append({**_step(f"inc{k}_build", "build", build_inputs,
                              nxt=[f"inc{k}_deploy"]),
                      "title": f"Increment {k} · Build"})
        steps.append({**_step(f"inc{k}_deploy", "deploy",
                              {"run_id": _from(f"inc{k}_build", "code_task_run_id")},
                              nxt=[after_deploy], approver_role="release_manager",
                              # "deploys them, then improves in steps": each increment goes
                              # live as the environment's policy says, and the plan says so.
                              follows_policy=True),
                      "title": f"Increment {k} · Deploy"})
    steps.append(_step("report", "report", nxt=["notify"]))
    steps.append(_step("notify", "notify", _notify_inputs()))
    return steps


def _reads(*kinds: str) -> list[dict[str, str]]:
    return [{"kind": k} for k in kinds]


def _writes(*specs: tuple) -> list[dict[str, Any]]:
    """`("agent_decision", True, "Assessment")` → a contract write: kind, required, decision_type."""
    out = []
    for kind, required, *rest in specs:
        entry: dict[str, Any] = {"kind": kind, "required": bool(required)}
        if rest and rest[0]:
            entry["decision_type"] = rest[0]
        out.append(entry)
    return out


# What Historical pattern actually calls (it lists matching tickets; it never fetches one by id).
_ITSM_CALLS = [{"mcp": "itsm", "tool": "list_incidents"}]

# STEP CONTRACTS (design §4.1, §5.1): what each stage of the KG investigation template reads
# from the Knowledge Graph, must write to the Context Graph, and calls. Core kinds only, so the
# template publishes into any project. A `required` write is one the step ALWAYS produces — the
# engine fails a step that does not (one repair round first); placeholders (`task` stages a
# bank fills with its own agent) declare their slot's writes as optional.
KG_TEMPLATE_CONTRACTS: dict[str, dict[str, Any]] = {
    "intake": {
        "trigger": "a person or monitoring opens a case on a subject node",
        "reads": _reads("incident", "rule"),
        "writes": _writes(("agent_decision", False, "CaseOpened")),
        "handoff": {"to": "evidence_loop", "carries": ["subject_id"]},
        "human_review": "none",
        "decisions": ["placeholder: an instance names the agent that opens or attaches the case"],
    },
    "context_enrichment": {
        "trigger": "inside evidence_loop, after intake; at most 2 rounds",
        "reads": _reads("rule", "symbol", "file", "service", "datastore", "team", "incident"),
        "writes": _writes(("agent_decision", True, "ContextAssembled"), ("coverage_gap", False)),
        "handoff": {"to": "historical_pattern", "carries": ["collected", "missing_kinds"]},
        "human_review": "none; what the hop cap could not reach is a CoverageGap and the run "
                        "continues",
        "policy": {"max_hops": 2},
        "llm": "none",
        "decisions": ["facts only: a fixed graph traversal, $0 and repeatable",
                      "missing expected kinds re-run the loop, at most twice"],
    },
    "evidence_loop": {
        "trigger": "after intake; re-runs context_enrichment while kinds are missing",
        "handoff": {"to": "historical_pattern", "carries": ["collected"]},
        "policy": {"max_iterations": 2},
        "llm": "none",
        "decisions": ["bounded: never more than 2 enrichment rounds"],
    },
    "historical_pattern": {
        "trigger": "after the evidence loop",
        "reads": _reads("incident", "precedent", "rule", "symbol"),
        "writes": _writes(("agent_decision", True, "HistoricalPatternMatched"),
                          ("incident", False)),
        "calls": _ITSM_CALLS,
        "handoff": {"to": "analysis", "carries": ["matched"]},
        "human_review": "none",
        "policy": {"max_hops": 2},
        "llm": "none",
        "decisions": ["a match shares a link with the subject; correlates_with is upserted, "
                      "never duplicated",
                      "an ITSM ticket naming the subject's code becomes an incident node"],
    },
    "analysis": {
        "trigger": "after historical_pattern",
        "reads": _reads("rule", "symbol", "incident", "precedent", "agent_decision"),
        "writes": _writes(("agent_decision", False, "Assessment")),
        "handoff": {"to": "orchestrator", "carries": ["findings", "finding_families"]},
        "human_review": "graph changes are proposals that go to review",
        "decisions": ["cites the evidence for each finding, with a confidence"],
    },
    "orchestrator": {
        "trigger": "after analysis",
        "reads": _reads("agent_decision"),
        "writes": _writes(("agent_decision", False, "Routed")),
        "handoff": {"to": "lanes", "carries": ["finding_families"]},
        "human_review": "none",
        "decisions": ["routes each finding family to its own lane"],
    },
    "lanes": {
        "trigger": "one remediation_advisor per finding family",
        "handoff": {"to": "human_approval", "carries": ["results"]},
        "policy": {"max_items": 25, "concurrency": 3},
        "llm": "none",
    },
    "remediation_advisor": {
        "trigger": "one per finding family (lanes)",
        "reads": _reads("rule", "symbol", "team", "agent_decision"),
        "writes": _writes(("agent_decision", False, "RemediationProposed")),
        "handoff": {"to": "human_approval", "carries": ["remediation"]},
        "human_review": "a person approves at human_approval",
    },
    "human_approval": {
        "trigger": "remediations are ready",
        "writes": _writes(("agent_decision", True, "Approved|Rejected")),
        "handoff": {"to": "execution", "carries": ["decision_id"]},
        "human_review": "the approver approves or rejects; a rejection stops the run",
        "llm": "none",
        "decisions": ["nothing executes before a person approves"],
    },
    "execution": {
        "trigger": "after approval",
        "handoff": {"to": "closure", "carries": ["execution_state"]},
        "human_review": "runs under the approver's grant; a different person merges",
    },
    "closure": {
        "trigger": "after execution",
        "reads": _reads("incident", "precedent"),
        "writes": _writes(("precedent", False), ("agent_decision", False, "PrecedentRecorded")),
        "handoff": {"to": "the next run's historical_pattern", "carries": ["precedent_id"]},
        "human_review": "none",
        "decisions": ["records the resolution as a precedent the next run finds"],
    },
}

# The regulatory instance (design §5.3, NorthPeak Lending on Apache Fineract). Every required
# write here is one the step's code really persists and names in its `produced` output — the
# engine verifies the row and stamps run/step/version/fingerprint on it.
REGULATORY_CONTRACTS: dict[str, dict[str, Any]] = {
    "clause_intake": {
        "trigger": "a run starts on a regulation document (document_id) or a clause (subject_id)",
        "reads": _reads("document", "clause", "regulation"),
        "writes": _writes(("compliance_case", True), ("agent_decision", True, "ClauseIntake")),
        "handoff": {"to": "evidence_loop", "carries": ["compliance_case_ids", "subject_id"]},
        "human_review": "none; the Compliance Officer reviews the outcome at human_approval",
        "llm": "none",
        "decisions": ["one ComplianceCase per clause of the document; with none, one for the "
                      "subject",
                      "re-opening an open case is idempotent (keyed by clause)"],
    },
    "evidence_loop": KG_TEMPLATE_CONTRACTS["evidence_loop"],
    "context_enrichment": {
        **KG_TEMPLATE_CONTRACTS["context_enrichment"],
        "trigger": "inside evidence_loop, after clause_intake; at most 2 rounds",
        "reads": _reads("clause", "rule", "symbol", "file", "system", "owner",
                        "regulatory_report"),
    },
    "historical_pattern": {
        **KG_TEMPLATE_CONTRACTS["historical_pattern"],
        "handoff": {"to": "compliance_assessment", "carries": ["matched"]},
    },
    "compliance_assessment": {
        "trigger": "after historical_pattern",
        "reads": _reads("clause", "rule", "symbol", "incident", "precedent"),
        "writes": _writes(("agent_decision", True, "Assessment")),
        "handoff": {"to": "compliance_orchestration",
                    "carries": ["assessment", "confidence", "citation", "cited_incident"]},
        "human_review": "implemented_by / gap edges are proposals that go to review",
        "decisions": ["implemented, partly implemented or gap, with a confidence",
                      "cites only a candidate node it was offered; anything else is a gap",
                      "a correlated incident's detail (INC-2088) comes from ITSM, never invented"],
    },
    "compliance_orchestration": {
        "trigger": "after compliance_assessment",
        "reads": _reads("agent_decision", "rule", "symbol"),
        "writes": _writes(("agent_decision", True, "Routed")),
        "handoff": {"to": "lanes", "carries": ["finding_families"]},
        "human_review": "none",
        "decisions": ["families: payment_crediting, payment_allocation, prepayment — the "
                      "Pipeline Orchestrator's judgement, never a keyword match",
                      "no fitting family starts no lane"],
    },
    "lanes": {
        **KG_TEMPLATE_CONTRACTS["lanes"],
        "trigger": "one lane_confirmation per finding family",
        "handoff": {"to": "remediation_package", "carries": ["results"]},
    },
    "lane_confirmation": {
        "trigger": "one per finding family (lanes)",
        "reads": _reads("clause", "rule", "symbol", "file", "incident"),
        "writes": _writes(("agent_decision", True, "Confirmed|RuledOut|CannotVerify")),
        "handoff": {"to": "remediation_package", "carries": ["verdict", "reason",
                                                             "cited_node_id"]},
        "human_review": "none",
        "policy": {"max_cited_symbols": 5},
        "decisions": ["judged against the current source lines, read-only",
                      "CannotVerify is forced by the code: no citation, no connected symbol, "
                      "or unreadable source",
                      "only a successful code read may rule a finding out"],
    },
    "remediation_package": {
        "trigger": "after the lanes",
        "reads": _reads("clause", "rule", "symbol", "owner", "incident", "agent_decision"),
        "writes": _writes(("remediation_package", True)),
        "handoff": {"to": "human_approval",
                    "carries": ["remediation_package_id", "owner_team"]},
        "human_review": "the Compliance Officer approves it at human_approval",
        "decisions": ["a change brief, a test, an owner team and an approver role",
                      "closes the gap the assessment states, in the code the lane read"],
    },
    "human_approval": {
        **KG_TEMPLATE_CONTRACTS["human_approval"],
        "trigger": "a remediation package is ready",
        "reads": _reads("remediation_package", "compliance_case"),
        "human_review": "the Compliance Officer approves or rejects; a rejection stops the run",
    },
    "execution": {
        **KG_TEMPLATE_CONTRACTS["execution"],
        "reads": _reads("remediation_package"),
        "handoff": {"to": "compliance_closure", "carries": ["execution_state"]},
    },
    "compliance_closure": {
        "trigger": "after execution (or approval, where execution is out of the workflow)",
        "reads": _reads("compliance_case", "incident", "precedent"),
        "writes": _writes(("precedent", True), ("agent_decision", True, "PrecedentRecorded")),
        "handoff": {"to": "the next run's historical_pattern", "carries": ["precedent_id"]},
        "human_review": "none",
        "llm": "none",
        "decisions": ["the precedent links to what the subject was connected to, so the next "
                      "run's Historical Pattern finds it",
                      "a reused edge back to every incident/precedent this run correlated"],
    },
}


def with_contracts(steps: list[dict[str, Any]], contracts: dict[str, dict[str, Any]]
                   ) -> list[dict[str, Any]]:
    """Each step with its declared contract — a step that already carries one keeps it (an
    outside agent's own contract, an operator's edit)."""
    return [({**s, "contract": contracts[s["id"]]}
             if s.get("id") in contracts and not s.get("contract") else s) for s in steps]


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
# one thing a person still decides: not every app that builds should ship. A Deploy step
# promises that person unless it says `follows_policy` (deploy_policy), and only workflows whose
# words promise nobody at the release say it — test_a_promised_approval_is_always_asked.
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
        # A LIGHTER PASS 2 FOR A HOTFIX (two_pass.LIGHT): regression tests around the fix and
        # its error paths, pass 1's budget, no browser suite. Not none: a fix with only its
        # happy-path test is how the same failure ships twice. Not the full pass: the release
        # waits on it, and a hotfix should ship fast.
        "more_tests": "light",
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
        # AN APP'S OWN ALERT, ANSWERED (services/incident_response.py). Started by an alert
        # rule whose Respond automatically is on (per rule, on the app's Alerts tab; never by
        # an app-wide event subscription — event_dispatch.PER_RULE_ONLY) or by Respond on the
        # incident; its input: incident_id, extends_run_id (the app's build lineage), title,
        # directive.
        "key": "incident_response",
        "name": "Incident Response",
        "description": "An app's alert fired. Investigate, fix the root cause with a regression "
                       "test, have someone approve the release, ship it, and prove recovery "
                       "from the monitor.",
        "objective": (
            "An app BOSS deployed is failing in front of users, and its own monitor raised the "
            "alarm. Find the root cause from the evidence — the app's metrics, its logs and the "
            "release that was serving — fix exactly that with a regression test that fails on "
            "the faulty release, and restore service. A person approves the release; recovery "
            "is judged by BOSS's monitor, not by the fix's author. If the evidence does not "
            "support a root cause, say so and stop rather than guess."
        ),
        "work_class": "bugfix",
        # NO SECOND PASS: users are failing now. The fix's own acceptance criteria demand the
        # regression test, and the build's gates run the app's whole existing suite; a More
        # tests pass and its second deploy would hold the recovery for another build.
        "more_tests": False,
        "steps": _chain([
            _step("investigate", "investigate_incident", {"incident_id": _in("incident_id")}),
            # The investigation's brief and the incident's acceptance criteria reach the build
            # the way a panel's PRD does: folded in by the runner from the step that produced
            # them (`incident_brief`), so this build binds what every catalogue build binds.
            _step("build", "build", _build_inputs()),
            _step("ci", "wait_for_ci", {"pull_request_url": _from("build", "pull_request_url")}),
            _step("report", "report"),
            # A PERSON OWNS AN INCIDENT FIX'S RELEASE (D2: "require CI success and operator
            # approval before deployment"), whatever the environment's policy lets a deploy do
            # unasked — on the laptop staging ships without asking, set 2026-10-12.
            _step("approve", "release_approval", approver_role="release_manager"),
            # THE PERSON IS THE GATE ABOVE, so this step follows the environment's policy
            # rather than asking the same person the same question twice.
            _step("deploy", "deploy", {"run_id": _from("build", "code_task_run_id")},
                  approver_role="release_manager", follows_policy=True),
            _step("verify", "verify_recovery", {"incident_id": _in("incident_id")}),
            _step("incident_report", "incident_report", {"incident_id": _in("incident_id")}),
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
                  approver_role="release_manager",
                  # Nothing here promises a person at the release: the policy decides.
                  follows_policy=True),
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
        # THE SECURITY REVIEW (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.2). Real tools find; a
        # panel chaired by the security lens recommends; a person chooses; the app's next build
        # fixes; the same tools re-scan; the re-review passes only when every targeted finding
        # is gone (or a person justified it) and no new high or critical appeared. "Changes
        # asked" runs a second, last round on what is left (fix_2 .. rereview_2) — a bounded
        # loop drawn as steps, each of which says "not needed" when round 1 passed. Hand-wired
        # through `_chain`; the literal `after` inputs name the step each one follows.
        "key": "security_review",
        "name": "Security Review",
        "description": "Scan an app with real tools, have a panel recommend fixes, choose them, "
                       "build them, and prove them with a re-scan.",
        "objective": (
            "Find real security flaws in this app with real tools, fix the ones a person "
            "chooses, and prove each fix with a re-scan. The panel's job is a short, "
            "prioritised list of fixes tied to the findings — not a product spec."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("scan", "security_scan", {"app_key": _in("app_key"), "zap_mode": _in("zap_mode")}),
            _step("panel", "security_panel", {"app_key": _in("app_key"),
                                              "focus": _in("directive")}),
            _step("decide", "security_decide", approver_role="security_reviewer"),
            _step("fix", "security_fix", {"app_key": _in("app_key"), "round": 1}),
            _step("rescan", "security_scan", {"app_key": _in("app_key"), "after": "fix"}),
            _step("rereview", "security_rereview", {"app_key": _in("app_key"), "after": "rescan",
                                                    "round": 1, "final": False}),
            _step("fix_2", "security_fix", {"app_key": _in("app_key"), "round": 2,
                                            "after_review": "rereview"}),
            _step("rescan_2", "security_scan", {"app_key": _in("app_key"), "after": "fix_2"}),
            _step("rereview_2", "security_rereview", {"app_key": _in("app_key"),
                                                      "after": "rescan_2", "round": 2,
                                                      "final": True}),
            _step("report", "security_report", {"app_key": _in("app_key")}),
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
                  approver_role="release_manager",
                  # Nothing here promises a person at the release: the policy decides.
                  follows_policy=True),
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
                  nxt=["document"], approver_role="release_manager",
                  # Nothing here promises a person at the release: the policy decides.
                  follows_policy=True),
            _step("document", "document", {"title": _in("title")}),
        ],
    },
    {
        "key": "incremental_build",
        "name": "Incremental Build",
        "description": "Builds the basics first, deploys them, then improves in steps.",
        "objective": (
            "Ship the app in visible increments: the basics live first, then improved in steps. "
            "The PRD MUST end with an increment plan, headed exactly '## Increment 1', "
            "'## Increment 2', … — at most " + str(4) + " increments. Increment 1 is the core "
            "of the brief (the smallest app a person can use end to end); each later increment "
            "is a small set of the remaining requirements, in the order they add the most. "
            "Each build builds only its own increment, on the branch the earlier ones left."
        ),
        "work_class": "project.new",
        "steps": _increments(4),
    },
    {
        # SCAN FOR BUSINESS RULES (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §4.5). Reads code, writes no
        # code: an agent proposes the rules an area or package enforces, with evidence, and a
        # person approves each one in Graph › Rules review. Started from the Graph area (an
        # area or package in the tree) or as a workflow.
        "key": "business_rules_scan",
        "name": "Scan for Business Rules",
        "description": "Read an area or package of the code and propose the business rules it "
                       "enforces, with the lines that prove them. A person approves each rule.",
        "objective": (
            "Find the business rules this part of the code enforces — validations, "
            "calculations, limits, state transitions, who may do what — and propose each one "
            "with the lines that enforce it. Propose only what the code shows; a person decides "
            "which rules are true. Nothing is built or changed."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("scan", "scan_business_rules", {
                "scope": _in("scope"), "focus": _in("directive"), "max_files": _in("max_files"),
                "max_rules": _in("max_rules"), "budget_usd": _in("budget_usd"),
                "exclude_tests": _in("exclude_tests")}),
            _step("review", "rules_review", {"scan_id": _from("scan", "scan_id")}),
        ]),
    },
    {
        # EXTRACT A RULEBOOK'S RULES (rulebook source G5). A document a person uploaded and
        # confirmed in Knowledge Graph › Documents is read by the Rulebook Analyst, a batch of
        # sections at a time; each rule it states is proposed with its thresholds and the clause
        # quoted as evidence, and a person approves each one in Rules review. Nothing is invented:
        # a rule id must be printed in the clause it cites.
        "key": "rulebook_extraction",
        "name": "Extract Rules from a Rulebook",
        "description": "Read a rulebook you uploaded and propose each data-quality rule it states, "
                       "with its Warning and Error thresholds, the data it protects and the clause "
                       "it came from. A person approves each rule.",
        "objective": (
            "Read every section of the named document and propose each rule in force that it "
            "states — its id, statement, dimension, severity, check, owner, the critical data "
            "elements and table.column values it protects, its regulatory reach, and its "
            "Warning and Error thresholds — quoting the clause word for word. Propose nothing the "
            "document does not print, and no retired rule. Nothing is approved automatically."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("extract", "extract_rulebook_rules", {
                "document_id": _in("document_id"), "budget_usd": _in("budget_usd")}),
            _step("review", "rules_review", {"scan_id": _from("extract", "scan_id")}),
        ]),
    },
    {
        # TRACE A REGULATION TO CODE (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md Domain layer, A2). An
        # agent reads a document's clauses and proposes the rule or code that implements each,
        # or a gap when nothing does. A person reviews every proposal; nothing is auto-approved.
        "key": "regulatory_trace",
        "name": "Trace Regulation to Code",
        "description": "Read a document's clauses and propose the rule or code that "
                       "implements each, or a gap when nothing does. A person reviews every "
                       "proposal.",
        "objective": (
            "For each clause of the named document, find what already implements it — a "
            "business rule or a piece of code — and propose that link with a short rationale. "
            "A clause nothing implements is a gap, a valid and expected outcome, not an error. "
            "Nothing proposed here is approved automatically."
        ),
        "work_class": "any",
        "steps": _chain([
            _step("trace", "trace_regulation", {
                "document_id": _in("document_id"), "directive": _in("directive"),
                "budget_usd": _in("budget_usd")}),
            _step("review", "regulatory_trace_review", {"trace_id": _from("trace", "trace_id")}),
        ]),
    },
    {
        # KG P5a (design: ~/boss-ops design §4.4 and §5.1). For a case's subject node — a
        # clause, say — gather what the graph already knows (rules, code, systems, owners,
        # reports) and what prior incidents it resembles, BEFORE any agent reasons about it:
        # two deterministic, $0 graph queries (`knowledge.graph_steps`), never a model call.
        # Analysis then judges the evidence the two deterministic steps assembled; a bounded
        # loop lets it ask for one more enrichment round, capped at 2, rather than guessing.
        # Findings fan out over Lanes (one remediation per finding family) and nothing executes
        # without a person's approval.
        "key": "kg_investigation_pipeline",
        "name": "KG Investigation Pipeline",
        "description": "Enrich a case's subject node from the Context Graph, match it against "
                       "prior incidents, have an agent analyse the evidence, then remediate "
                       "each finding family under human approval.",
        "objective": (
            "Investigate the named subject node: assemble its connected rules, code, systems, "
            "owners and reports (Context Enrichment) and the incidents or precedents it "
            "resembles (Historical Pattern) — both $0 graph queries, never a model call. "
            "Analyse the assembled evidence and cite it; ask for one more enrichment round, at "
            "most twice, when the evidence is insufficient. Route each finding family to its "
            "own remediation lane, and execute nothing without a person's approval. Closure "
            "records what was found as a precedent, so the next investigation finds it too."
        ),
        "work_class": "any",
        "steps": [
            {
                # AGENT STAGES ARE PLACEHOLDERS HERE (`task`, not an agent_work capability): the
                # instance agents that reason over Analysis/Orchestrator/Remediation/Execution/
                # Closure are P5b's SKILL.md roles, out of scope for P5a. A `task` step publishes
                # into any project without needing one already on its roster; swapping it for a
                # real agent_work or capability step is P5b's wiring, not this template's shape.
                "id": "intake", "kind": "task",
                "binding": {"inputs": {
                    "instructions": _in("directive"),
                    "subject_id": _in("subject_id")}},
                "next": ["evidence_loop"],
            },
            {
                # LOOP BODY ONLY: re-run up to twice (max_iterations) before Historical
                # Pattern and Analysis see its output — never reached by its own `next`.
                "id": "context_enrichment", "kind": "deterministic",
                "binding": {"inputs": {
                    "subject_id": _in("subject_id"),
                    "max_hops": {**_in("max_hops"), "default": 2}}},
                "next": [],
            },
            {
                "id": "evidence_loop", "kind": "bounded_loop", "body": "context_enrichment",
                "max_iterations": 2,
                "expression": {"path": "steps.context_enrichment.output.missing_kinds",
                               "op": "truthy"},
                "next": ["historical_pattern"],
            },
            {
                "id": "historical_pattern", "kind": "deterministic",
                "binding": {"inputs": {"subject_id": _in("subject_id")}},
                "next": ["analysis"],
            },
            {
                # THE ONE REAL AGENT STAGE HERE: a model reads the two deterministic steps'
                # evidence and reasons over it, so unlike its task-placeholder neighbours it
                # binds `agent_work` and needs a connected model (workflow_readiness.needs_of).
                "id": "analysis", "kind": "task",
                "binding": {"agent_work": True, "inputs": {
                    "instructions": (
                        "Read the Context Enrichment and Historical Pattern evidence for this "
                        "subject. Cite the evidence for each finding, with a confidence, and "
                        "group findings into families for remediation.")}},
                "next": ["orchestrator"],
            },
            {
                "id": "orchestrator", "kind": "task",
                "binding": {"inputs": {
                    "instructions": "Route each finding family from Analysis to its own "
                                    "remediation lane."}},
                "next": ["lanes"],
            },
            {
                "id": "lanes", "kind": "for_each",
                "items": "steps.orchestrator.output.finding_families", "body": "remediation_advisor",
                "max_items": 25, "concurrency": 3,
                "next": ["human_approval"],
            },
            {
                # FOR_EACH BODY ONLY: one remediation per finding family, never reached by
                # its own `next` (the for_each returns each run to `lanes`).
                "id": "remediation_advisor", "kind": "task",
                "binding": {"inputs": {
                    "instructions": "Propose the remediation for this finding family, citing "
                                    "the evidence it is based on."}},
                "next": [],
            },
            {
                "id": "human_approval", "kind": "decision",
                "question": "Approve the proposed remediations for execution?",
                "guidance": "Each remediation cites the evidence Analysis and the lanes "
                           "assembled; nothing here has been executed yet.",
                "approval": {"mode": "manual", "approver_role": "compliance_officer"},
                "next": ["execution"],
            },
            {
                "id": "execution", "kind": "task",
                "binding": {"inputs": {
                    "instructions": "Carry out the approved remediations."}},
                "next": ["closure"],
            },
            {
                "id": "closure", "kind": "task",
                "binding": {"inputs": {
                    "instructions": "Record this investigation's resolution as a precedent, so "
                                    "a later investigation's Historical Pattern step finds it."}},
                "next": [],
            },
        ],
    },
    {
        # KG P5b: the banking-regulatory instance of kg_investigation_pipeline. Same ten-stage
        # shape; context_enrichment and historical_pattern are P5a's deterministic steps,
        # reused unmodified. The six agent stages are real capability steps (services/
        # knowledge/banking_regulatory_pipeline.py), not `task` placeholders.
        "key": "banking_regulatory_compliance",
        "name": "Banking Regulatory Compliance",
        "description": "Trace a pasted regulation clause to the code and prior incidents that "
                       "show whether it is already handled, confirm each finding against the "
                       "code, and propose a remediation under a compliance officer's approval.",
        "objective": (
            "For each clause of the named regulation document, open a compliance case and "
            "assemble what the graph already knows about it — connected business rules, code, "
            "systems, owners and reports (Context Enrichment), and the incidents or precedents "
            "it resembles (Historical Pattern) — both $0 graph queries. Assess the evidence as "
            "implemented, partly implemented or a gap, with a confidence and a citation, citing "
            "a correlated incident's detail where one was found. Route the finding to its "
            "family's lane, confirm it against the code, and propose a remediation package. "
            "Nothing executes without the Compliance Officer's approval. Closure records the "
            "resolution as a precedent, so the next investigation finds it too."
        ),
        "work_class": "any",
        "steps": [
            _step("clause_intake", "clause_intake", {
                "document_id": _in("document_id"), "subject_id": _in("subject_id"),
                "directive": _in("directive")},
                nxt=["evidence_loop"]),
            {
                # LOOP BODY ONLY, same as kg_investigation_pipeline's: re-run up to twice
                # before Historical Pattern and the Analyst see its output.
                "id": "context_enrichment", "kind": "deterministic",
                "binding": {"inputs": {
                    "subject_id": _in("subject_id"),
                    "max_hops": {**_in("max_hops"), "default": 2}}},
                "next": [],
            },
            {
                "id": "evidence_loop", "kind": "bounded_loop", "body": "context_enrichment",
                "max_iterations": 2,
                "expression": {"path": "steps.context_enrichment.output.missing_kinds",
                               "op": "truthy"},
                "next": ["historical_pattern"],
            },
            {
                "id": "historical_pattern", "kind": "deterministic",
                "binding": {"inputs": {"subject_id": _in("subject_id")}},
                # Its contract (ITSM calls included) is REGULATORY_CONTRACTS', applied below.
                "next": ["compliance_assessment"],
            },
            _step("compliance_assessment", "compliance_assessment",
                 {"subject_id": _in("subject_id")}, nxt=["compliance_orchestration"]),
            _step("compliance_orchestration", "compliance_orchestration", {},
                 nxt=["lanes"]),
            {
                "id": "lanes", "kind": "for_each",
                "items": "steps.compliance_orchestration.output.finding_families",
                "body": "lane_confirmation", "max_items": 25, "concurrency": 3,
                "next": ["remediation_package"],
            },
            _step("lane_confirmation", "lane_confirmation", {}, nxt=[]),
            _step("remediation_package", "remediation_package", {}, nxt=["human_approval"]),
            {
                "id": "human_approval", "kind": "decision",
                "question": "Approve the proposed remediation package for execution?",
                # WHICH RULE, AGAINST WHICH CLAUSE — said by the graph nodes' names.
                "subject": ["{input.subject_id|name} against {input.document_id|attr:citation}",
                            "{input.subject_id|name}", "{input.document_id|name}"],
                "summary": ["{steps.remediation_package.output.note}"],
                "guidance": "The remediation cites the evidence Compliance Assessment and the "
                           "lane confirmation assembled; nothing has been executed yet. "
                           "Approving triggers a scoped code-change task; it does not merge it.",
                "approval": {"mode": "manual", "approver_role": "compliance_officer"},
                "next": ["execution"],
            },
            {
                "id": "execution", "kind": "task",
                "binding": {"inputs": {
                    "instructions": "Carry out the approved remediation package as a code "
                                    "task on the target repository."}},
                "next": ["compliance_closure"],
            },
            _step("compliance_closure", "compliance_closure", {
                "subject_id": _in("subject_id"), "symptom": _in("symptom"),
                "cause_summary": _in("cause_summary"),
                "execution_state": _in("execution_state")}, nxt=[]),
        ],
    },
    {
        # CASE STUDY 4: the AML alert-investigation instance of kg_investigation_pipeline
        # (services/knowledge/aml_alert_pipeline.py; design §5.3, mirrored for AML). One
        # monitoring alert: intake, evidence (KYC, transactions, past alerts through the sample's
        # connectors) and history are deterministic $0 steps; the Network Mapper, the AML Analyst
        # and the SAR Narrator are role agents; a PERSON decides at analyst_decision and a
        # person files at sar_filing. Closure turns the person's determination into a governed
        # precedent, so the next related alert's analysis starts with it. Installs only where
        # the AML sample is loaded (`requires`): its contracts name the banking-aml kinds and
        # the sample's connectors.
        "key": "aml_alert_investigation",
        "name": "AML Alert Investigation",
        "description": "Investigate a transaction-monitoring alert: gather KYC, transactions and "
                       "past alerts, map the related parties, recommend close or escalate citing "
                       "evidence and precedent — then a person decides, and a person files.",
        "objective": (
            "Investigate the named alert. Open its case, collect the evidence through the "
            "case-management, KYC and transactions connectors, map the parties related to the "
            "customer from the Knowledge Graph citing the edges, and find the closest prior "
            "determinations on that network. The AML Analyst recommends closing it as a false "
            "positive or escalating it, with a confidence, citing the evidence and any precedent "
            "it relied on. A person makes the determination and says why; agents never close an "
            "alert and never file a report. An escalation gets a drafted SAR narrative that a "
            "person reviews and files. The determination is recorded as precedent for the next "
            "related alert."
        ),
        "work_class": "any",
        "requires": {"ontology_pack": "banking-aml",
                     "mcp": ("case-management", "kyc", "transactions")},
        "steps": [
            {
                "id": "alert_intake", "kind": "deterministic",
                "title": "Alert intake",
                "binding": {"inputs": {"alert_id": _in("alert_id")}},
                "contract": {
                    "trigger": "a person starts an investigation of one open alert",
                    "reads": [{"kind": "alert"}, {"kind": "account", "via": "raised_on"},
                              {"kind": "customer", "via": "raised_on"},
                              {"kind": "typology", "via": "matches_typology"}],
                    "writes": [{"kind": "aml_case", "required": False},
                               {"kind": "agent_decision", "required": True,
                                "decision_type": "AlertIntake"},
                               {"kind": "coverage_gap", "required": False}],
                    "calls": [{"mcp": "case-management", "tool": "get_alert"}],
                    "handoff": {"to": "evidence_collection", "carries": ["case_id", "alert_id"]},
                    "human_review": "none; an alert missing from case management is a "
                                    "CoverageGap and the case still opens",
                    "policy": {"max_hops": 1},
                    "llm": "none",
                    "decisions": ["one case per alert; reopening reuses it"],
                },
                "next": ["evidence_collection"],
            },
            {
                "id": "evidence_collection", "kind": "deterministic",
                "title": "Evidence collection",
                "binding": {"inputs": {"case_id": _from("alert_intake", "case_id"),
                                       "alert_id": _from("alert_intake", "alert_id")}},
                "contract": {
                    "trigger": "after alert_intake opens the case",
                    "reads": [{"kind": "alert"}, {"kind": "customer"}, {"kind": "account"},
                              {"kind": "kyc_profile", "via": "has_kyc"}, {"kind": "typology"},
                              {"kind": "transaction", "via": "flags"}],
                    "writes": [{"kind": "agent_decision", "required": True,
                                "decision_type": "EvidenceAssembled"},
                               {"kind": "coverage_gap", "required": False}],
                    "calls": [{"mcp": "kyc", "tool": "get_kyc_profile"},
                              {"mcp": "transactions", "tool": "query_transactions"},
                              {"mcp": "case-management", "tool": "list_alerts"}],
                    "handoff": {"to": "network_mapping", "carries": ["kyc", "transactions",
                                                                     "past_alerts"]},
                    "human_review": "none; a connector that does not answer is a CoverageGap",
                    "policy": {"max_hops": 2, "change_lookback_days": 90, "list_cap": 500,
                               "data_access": "read_only"},
                    "llm": "none",
                    "decisions": ["read-only: the collector never writes to a bank system"],
                },
                "next": ["network_mapping"],
            },
            {**_step("network_mapping", "aml_network_mapping", {}, nxt=["historical_pattern"]),
             "title": "Network mapping",
             "contract": {
                 "trigger": "after the evidence is assembled",
                 "reads": [{"kind": "customer"}, {"kind": "account", "via": "owns"},
                           {"kind": "counterparty", "via": "transfers_to"},
                           {"kind": "customer", "via": "related_to"}],
                 "writes": [{"kind": "agent_decision", "required": True,
                             "decision_type": "NetworkMapped"}],
                 "handoff": {"to": "historical_pattern", "carries": ["related_party_ids"]},
                 "human_review": "agent-drafted related_to links stay inferred until a person "
                                 "confirms them",
                 "policy": {"max_hops": 2, "list_cap": 60},
                 "llm": "agent",
                 "decisions": ["every related party cites the graph edges that show it",
                               "an unreadable answer claims no related party"],
             }},
            {
                "id": "historical_pattern", "kind": "deterministic",
                "title": "Historical pattern",
                "binding": {"inputs": {
                    "capability": "aml_historical_pattern",
                    "case_id": _from("alert_intake", "case_id"),
                    "alert_id": _from("alert_intake", "alert_id"),
                    "related_party_ids": _from("network_mapping", "related_party_ids")}},
                "contract": {
                    "trigger": "after the network is mapped",
                    "reads": [{"kind": "precedent", "via": "about"}, {"kind": "alert"},
                              {"kind": "customer", "via": "related_to"}],
                    "writes": [{"kind": "agent_decision", "required": True,
                                "decision_type": "HistoricalPatternMatched"}],
                    "handoff": {"to": "aml_analysis", "carries": ["matched"]},
                    "human_review": "none",
                    "policy": {"list_cap": 3},
                    "llm": "none",
                    "decisions": ["a precedent matches on a shared network entity or typology"],
                },
                "next": ["aml_analysis"],
            },
            {**_step("aml_analysis", "aml_analysis", {}, nxt=["analyst_decision"]),
             "title": "AML analysis",
             "contract": {
                 "trigger": "after historical_pattern",
                 "reads": [{"kind": "alert"}, {"kind": "kyc_profile"}, {"kind": "typology"},
                           {"kind": "transaction"}, {"kind": "precedent"}],
                 "writes": [{"kind": "agent_decision", "required": True,
                             "decision_type": "Recommendation"}],
                 "handoff": {"to": "analyst_decision", "carries": ["recommendation",
                                                                   "confidence", "rationale"]},
                 "human_review": "analyst_decision: a person decides; this is a recommendation",
                 "llm": "agent",
                 "decisions": ["close_false_positive or escalate, with a confidence",
                               "cites only evidence it was given; uncited is no recommendation",
                               "says whether each prior decision still holds"],
             }},
            {
                "id": "analyst_decision", "kind": "decision",
                "title": "Analyst decision",
                # WHICH ALERT: two waiting at this gate read identically without it.
                "subject": _AML_ALERT_SUBJECT,
                "summary": ["{steps.aml_analysis.output.note}"],
                "question": "Decide this alert: close it as a false positive, or escalate it?",
                "guidance": "The AML Analyst's recommendation, its confidence and the evidence "
                            "and precedent it cites are above. Approve to accept the "
                            "recommendation, and write your reason in the note — it becomes the "
                            "precedent later alerts are judged against. To decide otherwise, "
                            "start your note with 'Escalate:' or 'Close:'. BOSS closes and files "
                            "nothing on its own.",
                "approval": {"mode": "manual", "approver_role": "human"},
                # The inbox draws a reason box for this gate (decision_actions): the note IS the
                # determination's reason, and the precedent later alerts are judged against.
                "recommendation": {
                    "reason_label": "Decide",
                    "reason_prompt": "Your determination and why — e.g. 'Close: documented "
                                     "payroll, register on file' or 'Escalate: cash just "
                                     "under the threshold, wired offshore'. A note that names "
                                     "neither accepts the AML Analyst's recommendation."},
                "next": ["narrative"],
            },
            {**_step("narrative", "sar_narrative", {}, nxt=["filing_route"]),
             "title": "SAR narrative",
             "contract": {
                 "trigger": "after a person's determination; drafts only when they escalated",
                 "reads": [{"kind": "alert"}, {"kind": "customer"}, {"kind": "transaction"},
                           {"kind": "aml_case"}],
                 "writes": [{"kind": "agent_decision", "required": False,
                             "decision_type": "Narrative"}],
                 "handoff": {"to": "sar_filing", "carries": ["narrative"]},
                 "human_review": "sar_filing: a person reviews the draft and files it",
                 "llm": "agent",
                 "decisions": ["a draft only; the agent never files"],
             }},
            {
                "id": "filing_route", "kind": "condition",
                "title": "Escalated?",
                "expression": {"path": "steps.narrative.output.determination", "op": "eq",
                               "value": "escalate"},
                "on_true": "sar_filing", "on_false": "not_escalated", "next": [],
            },
            {
                "id": "sar_filing", "kind": "decision",
                "title": "File the SAR",
                "subject": _AML_ALERT_SUBJECT,
                # The draft's opening lines on the card; the whole draft is on the run.
                "summary": ["{steps.narrative.output.narrative}",
                            "{steps.narrative.output.note}"],
                "question": "File the suspicious activity report for this alert?",
                "guidance": "The narrative above is an agent's DRAFT. BOSS does not file: review "
                            "it, file the report yourself, then approve here with the filing "
                            "reference in your note.",
                "approval": {"mode": "manual", "approver_role": "human"},
                "recommendation": {
                    "reason_label": "Filed",
                    "reason_prompt": "The filing reference, once you have filed it yourself "
                                     "(BOSS never files)."},
                "next": ["aml_closure"],
            },
            # ITS OWN "NO" STEP: the branch a condition does not take is marked skipped, and
            # closure is where both paths end.
            {"id": "not_escalated", "kind": "timer", "wait_seconds": 0, "binding": {},
             "title": "Closed, nothing to file", "next": ["aml_closure"]},
            {
                "id": "aml_closure", "kind": "deterministic",
                "title": "Closure",
                "binding": {"inputs": {"case_id": _from("alert_intake", "case_id"),
                                       "alert_id": _from("alert_intake", "alert_id")}},
                "contract": {
                    "trigger": "after a person's determination (and filing, when escalated)",
                    "reads": [{"kind": "aml_case"}, {"kind": "precedent"}],
                    "writes": [{"kind": "precedent", "required": False},
                               {"kind": "agent_decision", "required": True,
                                "decision_type": "PrecedentRecorded"},
                               {"kind": "coverage_gap", "required": False}],
                    "calls": [{"mcp": "case-management", "tool": "add_case_note"}],
                    "human_review": "refuses to run without the person's determination",
                    "llm": "none",
                    "decisions": ["the determination becomes a governed precedent, about the "
                                  "alert's network and typology",
                                  "consulted/reused edges record which earlier determinations "
                                  "this case used"],
                },
                "next": [],
            },
        ],
    },
]

# THE CONSULTANT'S DQ RCA AND REMEDIATION PIPELINE (Instance A of kg_investigation_pipeline,
# design §5.2) and its two side workflows — dq_catalogue.py holds their stages, contracts and
# agent designs. Each installs only where the NorthPeak Bank data platform is loaded
# (`requires`: the banking-dq pack and the DQ sample's connectors).
CATALOGUE.extend(dq_catalogue.entries(_step, _in))


def _staff_builds_by_app_kind() -> None:
    """Apply BUILD_AGENT_BY_ENTRY to the catalogue's build steps, once, at import."""
    for entry in CATALOGUE:
        builder = BUILD_AGENT_BY_ENTRY.get(entry["key"])
        for step in entry["steps"] if builder else ():
            if (step.get("binding") or {}).get("capability") in _BUILD_CAPABILITIES:
                _set_default_captain(step, builder)


_staff_builds_by_app_kind()


#: Which catalogue entries carry which step contracts (design §4.1): every step of the KG
#: investigation template and of its regulatory instance declares its footprint.
CONTRACTS_BY_ENTRY: dict[str, dict[str, dict[str, Any]]] = {
    "kg_investigation_pipeline": KG_TEMPLATE_CONTRACTS,
    "banking_regulatory_compliance": REGULATORY_CONTRACTS,
}

#: The ontology pack an entry's contracts name kinds from. Seeding skips such an entry in a
#: project that has not set the pack (its kinds would be refused at publish) instead of failing
#: the whole catalogue; setting the pack seeds it (knowledge_project_tools._set_ontology_pack).
ONTOLOGY_PACK_BY_ENTRY: dict[str, str] = {
    "banking_regulatory_compliance": "banking-regulatory",
}


def _declare_contracts() -> None:
    for entry in CATALOGUE:
        contracts = CONTRACTS_BY_ENTRY.get(entry["key"])
        if contracts:
            entry["steps"] = with_contracts(entry["steps"], contracts)


_declare_contracts()


def _two_pass_builds() -> None:
    """EVERY BUILD IS FOLLOWED BY ITS MORE TESTS PASS (two_pass), applied to the catalogue once,
    at import, by the same rule a run applies to any workflow — so a card, the Studio and the
    seeded version all show the step, and no entry wires it by hand. QA is its soft default."""
    for entry in CATALOGUE:
        spec = {"steps": entry["steps"]}
        if "more_tests" in entry:
            spec["more_tests"] = entry["more_tests"]
        entry["steps"] = two_pass.with_more_tests(
            spec, captains={k: DEFAULT_STEP_AGENTS[k] for k in ("more_tests", "preview")})["steps"]


_two_pass_builds()


def _monitoring_before_release() -> None:
    """SET UP MONITORING IN FRONT OF EVERY SECOND DEPLOY (monitoring_pack, BOSSAPP-601): every
    catalogue workflow that deploys twice gets Beacon's pack after pass 2 and before the
    release, so the release is approved with its rules. Applied once, at import, after pass 2
    is wired, like `_two_pass_builds`."""
    from . import monitoring_pack

    for entry in CATALOGUE:
        entry["steps"] = monitoring_pack.with_monitoring_step(
            {"steps": entry["steps"]}, captain=DEFAULT_STEP_AGENTS["monitoring_pack"])["steps"]


_monitoring_before_release()


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
    "app_key": {"type": "string", "title": "App",
                "description": "The app to review: its id in this project (Factory › Apps). "
                               "Its newest build's branch is scanned.",
                "examples": ["shift-swap-board"]},
    "zap_mode": {"type": "string", "enum": ["baseline", "api"],
                 "title": "Running-app scan (optional)",
                 "description": "baseline (default) watches the app passively; api also "
                                "attacks its API, only inside the scan's own sandbox."},
    "incident_id": {"type": "string", "title": "Incident",
                    "description": "The incident to respond to (Observability › the app's "
                                   "alert). An alert rule starts this for you when its "
                                   "Respond automatically is on (the app's Alerts tab).",
                    "examples": ["inc_0123456789ab"]},
    "scope": {"type": "array", "items": {"type": "string"}, "title": "What to scan",
              "description": "Areas, packages, classes, files or folders: a key from the "
                             "Graph's project tree (area:<repo>:<area>, "
                             "pkg:<repo>:<area>:<package>, module:<repo>:<package>, a class's "
                             "symbol: key, file:<repo>:<path>) or a plain repository path to a "
                             "file or folder. Checked when the scan starts. Start the scan from "
                             "the tree to have this filled in.",
              "examples": [["pkg:owner/app:core:com.example.loans"],
                           ["core/src/main/java/com/example/loans"]]},
    "exclude_tests": {"type": "boolean", "default": True,
                      "title": "Leave test code out (optional)",
                      "description": "Test code exercises rules; it does not enforce them. On "
                                     "by default; turn it off to read the tests too."},
    "max_files": {"type": "integer", "minimum": 1, "maximum": 500, "default": 60,
                  "title": "Most files to read (optional)",
                  "description": "The scan stops after reading this many files. Default 60."},
    "max_rules": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25,
                  "title": "Most rules to propose (optional)",
                  "description": "The scan stops after proposing this many rules. Default 25."},
    "budget_usd": {"type": "number", "exclusiveMinimum": 0, "default": 2.0,
                   "title": "Budget in dollars (optional)",
                   "description": "The most the scan may spend on its model, at most the "
                                  "project's cap. Default $2.00."},
    "document_id": {"type": "string", "title": "Document (optional)",
                    "description": "The document to trace (Graph › Knowledge Graph › "
                                   "Documents). Leave blank and the trace reports nothing to "
                                   "do — add a document first.",
                    "examples": ["kgn_0123456789ab"]},
    "symptom": {"type": "string", "title": "What went wrong (optional)",
               "description": "A short description of the compliance gap or incident this "
                              "investigation closes, for the precedent it records.",
               "examples": ["Payment misapplied to principal instead of interest-then-"
                            "principal order"]},
    "cause_summary": {"type": "string", "title": "Root cause (optional)",
                      "description": "Why it happened, for the precedent a later "
                                     "investigation's Historical Pattern step will find."},
    "execution_state": {"type": "string", "enum": ["proposed", "merged", "unknown"],
                        "default": "proposed",
                        "title": "Execution state (optional)",
                        "description": "Whether the remediation's code task has been merged "
                                       "yet — the run finishing is not the same as the fix "
                                       "shipping."},
    "notify_channel": {"type": "string", "title": "Who to tell",
                       "description": "Where the outcome is posted: email:someone@example.com or "
                                      "mattermost:<channel id>. Leave blank and the notify step "
                                      "is skipped and says so.",
                       "examples": ["email:team@example.com"]},
    "subject_id": {"type": "string", "title": "Subject node",
                   "description": "The case's subject node to investigate (Graph › Knowledge "
                                  "Graph) — a clause, an incident, or whatever this case is "
                                  "about.",
                   "examples": ["kgn_0123456789ab"]},
    "alert_id": {"type": "string", "title": "Alert",
                 "description": "The transaction-monitoring alert to investigate (Knowledge "
                                "Graph › Documents › AML sample lists them), e.g. ALERT-1.",
                 "examples": ["ALERT-1"]},
    "max_hops": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2,
                 "title": "Hops to enrich (optional)",
                 "description": "How far Context Enrichment walks from the subject node. "
                                "Default 2."},
}

#: Per template: how its brief is asked for, an example that fits its purpose (the form's
#: placeholder and the explicit "try an example"), and any input required beyond what its steps
#: demand. Every CATALOGUE key must be here — `test_every_template_asks_for_its_own_brief`.
BRIEFS: dict[str, dict[str, Any]] = {
    "incremental_build": {
        "directive": ("What should it do?",
                      "Who it is for and what a person can do with it. The panel splits it into "
                      "increments: the basics go live first, the rest arrive in steps."),
        "example": {"title": "Team lunch poll",
                    "directive": "A lunch poll for a team: anyone proposes places, everyone "
                                 "votes once, the poll closes at 11:30 and shows the winner. "
                                 "Later: recurring polls, dietary tags and a Slack reminder."},
    },
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
    "incident_response": {
        "directive": ("Which incident?",
                      "Started from an app's alert: the incident names the app, the release "
                      "that was serving and what failed."),
        "example": {"title": "Order API: 18% of requests failing",
                    "directive": "Respond to the incident: Order API (staging): 18% of "
                                 "requests failing."},
        "required": ("incident_id",),
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
    "security_review": {
        "directive": ("What should the review focus on? (optional)",
                      "The scan covers the whole app; say what worries you most and the panel "
                      "weighs it."),
        "example": {"title": "Security review: Shift swap board",
                    "directive": "Check the approvals flow for injection and missing headers.",
                    "app_key": "shift-swap-board"},
        "required": ("app_key",),
    },
    "business_rules_scan": {
        "directive": ("What should the scan look for? (optional)",
                      "The scan proposes every business rule it finds in the scope. Name what "
                      "matters most to you and the agent reads for that first."),
        "example": {"title": "Rules in loan disbursement",
                    "directive": "Which dates, amounts and loan states allow a disbursement, and "
                                 "who may approve one.",
                    "scope": ["pkg:owner/app:core:com.example.loans.disbursement"]},
        "required": ("scope",),
    },
    "rulebook_extraction": {
        "directive": ("Anything to read for first? (optional)",
                      "The Rulebook Analyst proposes every rule the document states. Upload and "
                      "confirm the document in Knowledge Graph › Documents first; Extract rules "
                      "there starts this workflow with it."),
        "example": {"title": "Rules of the Loan DQ Rulebook v3.2",
                    "directive": "Read every rule card; the retired rules in the change log are "
                                 "not in force.",
                    "document_id": "kgn_rulebookdemo01"},
    },
    "regulatory_trace": {
        "directive": ("What should the trace focus on? (optional)",
                      "The trace proposes an implemented_by link or a gap for every clause of "
                      "the document. Name what matters most and the agent reads for that "
                      "first."),
        "example": {"title": "Trace Reg Z 1026.36(c) to repayment code",
                    "directive": "Check whether our repayment service credits payments as of "
                                 "the date of receipt and handles prepayments the way this "
                                 "clause requires.",
                    "document_id": "kgn_regz102636demo01"},
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
    "kg_investigation_pipeline": {
        "directive": ("What should this investigation focus on? (optional)",
                      "Context Enrichment and Historical Pattern read the subject node's own "
                      "graph; say what matters most and Analysis reads for that first."),
        "example": {"title": "Investigate Reg Z 1026.36(c) clause",
                    "directive": "Check whether the connected repayment code and any prior "
                                 "incidents already cover crediting payments as of the date of "
                                 "receipt.",
                    "subject_id": "kgn_regz102636demo01"},
        "required": ("subject_id",),
    },
    "aml_alert_investigation": {
        "directive": ("What should this investigation focus on? (optional)",
                      "The alert's own evidence is always collected; say what matters most."),
        "example": {"title": "Investigate ALERT-1", "alert_id": "ALERT-1",
                    "directive": "The payroll fan-out on Harbor Point Cafe's payroll account."},
        "required": ("alert_id",),
    },
    "banking_regulatory_compliance": {
        "directive": ("What should this compliance check focus on? (optional)",
                      "Context Enrichment and Historical Pattern read the clause's own graph; "
                      "say what matters most and the Compliance Assessment reads for that first."),
        "example": {"title": "Reg Z 1026.36(c)(1)(i) repayment order",
                    "directive": "Check whether the connected repayment code and INC-2088's "
                                 "incident already cover crediting payments interest-first.",
                    "document_id": "kgn_regz102636doc001",
                    "subject_id": "kgn_regz102636demo01"},
        "required": ("subject_id",),
    },
    **dq_catalogue.BRIEFS,
}
_FIELD_TEXT.update(dq_catalogue.FIELD_TEXT)


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
    spec = {"entry_step": entry["steps"][0]["id"], "steps": entry["steps"],
            "objective": entry.get("objective") or "",
            "workflow_name": entry["name"],
            "input_schema": input_schema_for(entry)}
    # AN OPT-OUT TRAVELS WITH THE SPEC. Pass 2 is re-applied where a version becomes a run's
    # steps (workflows._published_spec); a "light" pass survives because its steps are already
    # in the list, but "no pass 2" is an absence, and without the flag the run added a full
    # pass and a second deploy to the template that had said no.
    if entry.get("more_tests") is False:
        spec["more_tests"] = False
    # `monitoring_gate`: block (the default) or warn (monitoring_pack.gate_mode).
    if entry.get("monitoring_gate"):
        spec["monitoring_gate"] = entry["monitoring_gate"]
    # THE PIPELINE'S SHAPE BEYOND ITS STEPS (the DQ pipeline): the loop back, the side
    # workflows and paths, and the shared services, so the Pipeline view can draw them.
    if entry.get("pipeline"):
        spec["pipeline"] = entry["pipeline"]
    return spec


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

        # A DEPLOY THAT FOLLOWS THE ENVIRONMENT'S POLICY is nobody's gate until a project's
        # policy says (GET /v1/delivery-workflows/asks reads it): listed apart, never promised.
        from .deploy_policy import follows_policy

        policy_decides = [s["id"] for s in manual if follows_policy(s)]
        gates = [s["id"] for s in manual if s["id"] not in policy_decides
                 and (_capability(s) not in CAPABILITIES
                      or stops_for_a_person(_capability(s), (s.get("approval") or {}).get("mode"),
                                            prd_auto_approve=prd_auto_approve))]
        automatic = [s["id"] for s in manual if s["id"] not in gates
                     and s["id"] not in policy_decides]
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
            "policy_decides": policy_decides,
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


def seed_unlocked_by_pack(connection: Connection, project_id: str, pack_id: str,
                          actor: str = "boss") -> dict[str, Any]:
    """Install the catalogue entries a just-set ontology pack unlocks (their step contracts name
    its kinds), and nothing else. Idempotent."""
    keys = frozenset(k for k, p in ONTOLOGY_PACK_BY_ENTRY.items() if p == pack_id)
    if not keys:
        return {"project_id": project_id, "installed": [], "count": 0, "mapped": []}
    return seed(connection, project_id, actor=actor, only=keys)


def seed(connection: Connection, project_id: str, actor: str = "boss",
         only: Optional[frozenset[str]] = None) -> dict[str, Any]:
    """Install the catalogue into a project (or only the `only` entries). Idempotent.

    Re-running publishes a new version ONLY when the spec actually changed — the runtime keys
    versions by content hash, so an unchanged catalogue re-seeds to the same version rather
    than accumulating identical ones on every boot.
    """
    installed: list[dict[str, Any]] = []
    # THE ROLE AGENTS AN ENTRY'S STEPS ARE THE JUDGEMENT OF ARE INSTALLED WITH IT (Compliance
    # Analyst, Lane Agent, AML Analyst, ...): no core cast ships them, so without this
    # `_staffed_for` dropped every one and the run (and its consent) fell back to other agents.
    # Through the shipped-cast install path: additive, an operator's delete holds.
    wanted = [role for entry in CATALOGUE
              if (only is None or entry["key"] in only)
              and not _unmet(connection, project_id, entry)
              for role in roles_needed(entry)]
    if wanted:
        from .shipped_cast import install_roles

        install_roles(connection, project_id, wanted, actor=actor)
    # THE AGENTS THIS PROJECT ACTUALLY HAS, not the bundled roster: a step naming an agent
    # with no roles row is refused at start ("there is no agent 'sre' on this project"), so a
    # default agent is named only where it exists.
    present = frozenset(str(k) for k in connection.execute(
        text("SELECT role_key FROM roles WHERE project_id = :p"), {"p": project_id},
    ).scalars().all())
    for entry in CATALOGUE:
        if only is not None and entry["key"] not in only:
            continue
        unmet = _unmet(connection, project_id, entry)
        if unmet:
            # NOT HERE YET, AND SAID SO. An entry whose contracts name a pack's kinds or a
            # sample's connectors cannot publish into a project without them; it installs when
            # they arrive (setting the pack, or the AML sample's load, seeds the catalogue again).
            installed.append({"key": entry["key"], "version_id": None, "version": None,
                              "skipped": unmet})
            continue
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
    mapped = _seed_default_mappings(connection, project_id, actor=actor) if only is None else []
    return {"project_id": project_id, "installed": installed, "count": len(installed),
            "mapped": mapped}


def _unmet(connection: Connection, project_id: str, entry: dict[str, Any]) -> Optional[str]:
    """Why this project cannot have the entry yet (its `requires`, or its contracts' pack)."""
    unmet = unmet_requirements(connection, project_id, entry)
    needs_pack = ONTOLOGY_PACK_BY_ENTRY.get(entry["key"])
    if not unmet and needs_pack and not _has_pack(connection, project_id, needs_pack):
        unmet = f"needs the '{needs_pack}' ontology pack"
    return unmet


def restaff_role_agent_entries(connection: Connection, project_id: str,
                               actor: str = "boss") -> Optional[dict[str, Any]]:
    """Re-seed the catalogue entries THIS PROJECT ALREADY HAS whose steps are role agents'
    judgement, so a project that installed them before their steps named the role agents (or
    before the roles were installed) is backfilled on the next boot (shipped_cast.sync_project).
    Idempotent: an unchanged spec publishes nothing. None when the project has none of them."""
    keys = frozenset(e["key"] for e in CATALOGUE if roles_needed(e))
    have = frozenset(str(k) for k in connection.execute(
        text("SELECT workflow_key FROM workflow_definitions WHERE project_id = :p "
             "AND workflow_key = ANY(:k)"), {"p": project_id, "k": sorted(keys)}).scalars())
    if not have:
        return None
    return seed(connection, project_id, actor=actor, only=have)


def _has_pack(connection: Connection, project_id: str, pack_id: str) -> bool:
    """``pack_id`` is among the project's packs, or one of them includes it (banking-aml
    includes banking-regulatory, so a project running both case studies keeps the regulatory
    workflow; adding banking-dq keeps both)."""
    from .knowledge.ontology_packs import has_pack

    return has_pack(connection, project_id=project_id, pack_id=pack_id)


def unmet_requirements(connection: Connection, project_id: str,
                       entry: dict[str, Any]) -> Optional[str]:
    """What a catalogue entry's `requires` asks of the project and it lacks, in words, or None."""
    requires = entry.get("requires") or {}
    if not requires:
        return None
    from .mcp_registry import find_mcp_server

    pack_id = requires.get("ontology_pack")
    if pack_id and not _has_pack(connection, project_id, pack_id):
        return f"needs the {pack_id} ontology pack"
    missing = [name for name in requires.get("mcp") or ()
               if find_mcp_server(connection, project_id, name) is None]
    if missing:
        return f"needs the {', '.join(missing)} connector(s)"
    return None


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
