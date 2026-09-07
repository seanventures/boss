"""What a workflow step can actually DO.

A step could already call a skill, an integration or an agent group. None of those says
"research it", "convene the panel", "build it", "ship it", "watch it" — so the five things a
delivery workflow is made of were exactly the five things a step could not be.

This is the registry that closes it. Each capability declares what it needs, what it produces,
which tool performs it, and how risky it is. One declaration feeds four things that would
otherwise drift apart:

  * the builder's palette (what a customer can drag onto a canvas),
  * publish-time validation (whether this project can satisfy the step at all),
  * the runner (what to execute when the step becomes ready),
  * the approval floor (what a workflow is not allowed to auto-approve).

EXECUTION IS A TOOL CALL, DELIBERATELY. Every capability below maps onto a tool BOSS already
has, already governs by risk tier, and already uses in production from the chat agent and the
MCP surface. A second execution path for the same actions would be a second place for
"deploy" to mean something slightly different.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

#: Risk at or above this is ALWAYS a human decision, whatever a workflow says.
#: See `approval_mode_for` — governance that configuration can switch off is not governance.
MANUAL_APPROVAL_RISK_FLOOR = 2

#: What one customer-defined step is assumed to take. It calls a single tool, so minutes rather
#: than the tens of minutes a build or a deploy takes.
CUSTOM_STEP_MINUTES = 3


@dataclass(frozen=True)
class Capability:
    """One thing a workflow step can do."""

    key: str
    label: str
    #: One line, in the words an operator would use. Shown in the builder palette.
    summary: str
    #: The tool that performs it. None = the step is a marker the runtime resolves itself
    #: (an approval gate, say), not something dispatched to a tool.
    tool: Optional[str]
    #: 0 reads, 1 spends or writes something recoverable, 2+ changes the world in a way an
    #: operator would want to have agreed to first.
    risk_tier: int
    #: Inputs the step must be given, by name. Missing ones fail at PUBLISH time with a
    #: readable reason rather than at 2am in the middle of a run.
    required_inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()
    #: What downstream steps can read out of this one's output.
    produces: tuple[str, ...] = ()
    #: What must be true of the PROJECT for this step to be runnable at all — a repo bound, a
    #: deploy target chosen. Checked at publish time.
    requires_project: tuple[str, ...] = ()
    #: Skills this capability leans on, for display and for the "can this project do it" check.
    skills: tuple[str, ...] = ()
    #: Roughly how long, so a plan can be read before it is run. Not enforced.
    typical_minutes: int = 0
    #: True when the step waits on a person and cannot be executed by a runner at all.
    is_gate: bool = False
    #: Long-running work whose completion arrives later. The runner starts it and the step
    #: stays `waiting` until the thing it started reports back.
    is_async: bool = False


def _cap(**kwargs: Any) -> Capability:
    return Capability(**kwargs)


#: The catalogue of what a step can be. Ordered as a delivery flow reads, because this is also
#: the order the builder palette shows them in.
CAPABILITIES: dict[str, Capability] = {
    c.key: c
    for c in (
        _cap(
            key="research",
            label="Research",
            summary="Web research into a cited dossier, gated on real evidence.",
            tool="create_discovery",
            risk_tier=1,
            required_inputs=("seed",),
            optional_inputs=("title", "context", "deep"),
            produces=("discovery_id", "dossier"),
            typical_minutes=15,
            is_async=True,
        ),
        _cap(
            key="meeting",
            label="Convene a panel",
            summary="Agent panel debates the directive and produces a PRD.",
            tool="request_meeting",
            risk_tier=1,
            required_inputs=("directive",),
            # `research` is what makes the difference between a panel arguing from a one-line
            # directive and one arguing from evidence. Optional, because a workflow may skip
            # research entirely — but bindable, so one that does not can hand it over.
            optional_inputs=("task_id", "research"),
            produces=("meeting_run_id", "prd_markdown", "task_id"),
            typical_minutes=20,
            is_async=True,
        ),
        _cap(
            key="spec_review",
            label="Approve the spec",
            summary="A person reads the PRD and decides whether it gets built.",
            tool=None,
            risk_tier=2,
            required_inputs=(),
            optional_inputs=("approver_role",),
            produces=("decision_id",),
            is_gate=True,
        ),
        _cap(
            key="build",
            label="Build, review and test",
            summary="The whole inner pipeline: implement, adversarial review, security check, "
                    "run the tests, open a gated pull request. Succeeds when a PR exists.",
            tool="create_code_task",
            risk_tier=1,
            required_inputs=("title", "instructions"),
            optional_inputs=("acceptance_criteria", "test_commands", "task_id", "repository",
                             "reference_links"),
            produces=("code_task_run_id", "pull_request_url"),
            requires_project=("repository",),
            skills=("github.create_pr_real", "software_factory.sandbox_validation"),
            typical_minutes=45,
            is_async=True,
        ),
        _cap(
            key="review",
            label="Adversarial review",
            summary="Reports what the build's own reviewers found. The verdict belongs to the "
                    "build step — this only surfaces it.",
            tool=None,
            risk_tier=1,
            optional_inputs=("seats",),
            produces=("verdicts",),
            typical_minutes=10,
            is_async=True,
        ),
        _cap(
            key="test",
            label="Verify",
            summary="Reports the build's own test outcome. The verdict belongs to the build "
                    "step — this only surfaces it.",
            tool=None,
            risk_tier=0,
            optional_inputs=("commands",),
            produces=("tests_passed", "evidence"),
            typical_minutes=5,
            is_async=True,
        ),
        _cap(
            key="preview",
            label="Preview",
            summary="Start a live preview of the app so a person can use it.",
            tool="start_preview",
            risk_tier=1,
            required_inputs=("run_id",),
            optional_inputs=("ttl_minutes",),
            produces=("preview_id", "preview_url"),
            typical_minutes=5,
            is_async=True,
        ),
        _cap(
            key="ux_review",
            label="UX sign-off",
            summary="A person uses the preview and signs off on how it looks and behaves.",
            tool=None,
            risk_tier=2,
            optional_inputs=("approver_role",),
            produces=("decision_id",),
            is_gate=True,
        ),
        _cap(
            key="security_review",
            label="Security sign-off",
            summary="A person accepts the security posture of the change.",
            tool=None,
            risk_tier=3,
            optional_inputs=("approver_role",),
            produces=("decision_id",),
            is_gate=True,
        ),
        _cap(
            key="plrr",
            label="Production launch readiness",
            summary="Review the built app against the operability the spec promised, and "
                    "close the gaps before it ships.",
            tool=None,
            risk_tier=1,
            required_inputs=("run_id",),
            optional_inputs=("directive",),
            produces=("findings", "gaps_closed", "code_task_run_id"),
            typical_minutes=12,
            is_async=True,
        ),
        _cap(
            key="deploy",
            label="Deploy",
            summary="Ship the built app to a bound target.",
            tool="deploy_app",
            risk_tier=2,
            required_inputs=("run_id",),
            optional_inputs=("environment", "target_id"),
            produces=("deployment_id", "deployed_url"),
            requires_project=("deployment_target",),
            typical_minutes=8,
            is_async=True,
        ),
        _cap(
            key="monitor",
            label="Set up monitoring",
            summary="Register health checks so the app is watched after it ships.",
            tool=None,
            risk_tier=1,
            optional_inputs=("interval_minutes", "thresholds"),
            produces=("monitor_id",),
            typical_minutes=3,
        ),
        _cap(
            key="notify",
            label="Notify",
            summary="Post the outcome where the team will see it.",
            tool=None,
            risk_tier=1,
            required_inputs=("message",),
            optional_inputs=("channel",),
            produces=("posted",),
            skills=("mattermost.post_real",),
            typical_minutes=1,
        ),
        _cap(
            key="smoke_test",
            label="Smoke test",
            summary="Prove the deployed thing actually answers before anyone celebrates.",
            tool="http_probe",
            risk_tier=0,
            required_inputs=("url",),
            optional_inputs=("expect_status",),
            produces=("reachable", "status_code", "latency_ms"),
            typical_minutes=1,
        ),
        _cap(
            key="redeploy",
            label="Ship it again",
            summary="Re-ship the SAME build after a failed deploy. No rebuild, no model spend.",
            tool="redeploy",
            risk_tier=2,
            required_inputs=("run_id",),
            optional_inputs=("target_id",),
            produces=("deployment_id",),
            requires_project=("deployment_target",),
            typical_minutes=5,
            is_async=True,
        ),
        _cap(
            key="retry_build",
            label="Retry the build",
            summary="Run the coding task again when it failed for a reason that has passed.",
            tool="retry_code_task",
            risk_tier=1,
            required_inputs=("run_id",),
            produces=("code_task_run_id",),
            typical_minutes=45,
            is_async=True,
        ),
        _cap(
            key="teardown_preview",
            label="Stop the preview",
            summary="Release the sandbox once nobody needs to look at it.",
            tool="stop_preview",
            risk_tier=1,
            required_inputs=("preview_id",),
            produces=("stopped",),
            typical_minutes=1,
        ),
        _cap(
            key="cost_check",
            label="Check the spend",
            summary="What this work has cost so far, before deciding whether to continue.",
            tool="get_costs",
            risk_tier=0,
            optional_inputs=("since",),
            produces=("spend_usd",),
            typical_minutes=1,
        ),
        _cap(
            key="document",
            label="Document",
            summary="Write the change record from what this run produced.",
            tool=None,
            risk_tier=1,
            # `title` WAS REQUIRED, which made the step that writes the record the one step a
            # run could be started without satisfying: a run with no `title` input raises
            # before the writer is reached. The record can name itself from the directive, so
            # a missing title should cost a nicer heading, not the whole document.
            required_inputs=(),
            optional_inputs=("title", "markdown"),
            produces=("document_id",),
            typical_minutes=3,
        ),
    )
}

CAPABILITY_KEYS = frozenset(CAPABILITIES)


def get(key: str) -> Capability:
    try:
        return CAPABILITIES[key]
    except KeyError:
        raise KeyError(
            f"unknown capability '{key}'. Known: {', '.join(sorted(CAPABILITY_KEYS))}"
        ) from None


def palette() -> list[dict[str, Any]]:
    """Everything a builder needs to draw the palette, in flow order."""
    return [
        {
            "key": c.key,
            "label": c.label,
            "summary": c.summary,
            "risk_tier": c.risk_tier,
            "required_inputs": list(c.required_inputs),
            "optional_inputs": list(c.optional_inputs),
            "produces": list(c.produces),
            "requires_project": list(c.requires_project),
            "skills": list(c.skills),
            "typical_minutes": c.typical_minutes,
            "is_gate": c.is_gate,
            "is_async": c.is_async,
            # What the builder must show as fixed rather than as a choice.
            "approval_forced_manual": c.risk_tier >= MANUAL_APPROVAL_RISK_FLOOR,
        }
        for c in CAPABILITIES.values()
    ]


def approval_mode_for(capability_key: str, requested: Optional[str]) -> str:
    """The approval mode this step will ACTUALLY run under.

    A workflow may ask for `auto`; a capability at or above the risk floor gets `manual`
    regardless. Deploying to production and merging a pull request are not things a
    configuration file gets to decide were fine.

    Returning the effective value — rather than validating the requested one and storing it
    unchanged — means the stored plan says what will happen. A plan that reads `auto` next to a
    step that will stop and wait is a plan nobody can trust.
    """
    capability = get(capability_key)
    if capability.risk_tier >= MANUAL_APPROVAL_RISK_FLOOR:
        return "manual"
    mode = (requested or "auto").strip().lower()
    if mode not in ("auto", "manual"):
        raise ValueError("approval mode must be 'auto' or 'manual'.")
    return mode


def validate_step(step: Mapping[str, Any]) -> list[str]:
    """Everything wrong with one capability step, as sentences an operator can act on.

    Returns a list rather than raising on the first problem: a builder showing one error at a
    time makes a customer fix a workflow five times to publish it once.
    """
    problems: list[str] = []
    binding = step.get("binding") or {}
    key = str(binding.get("capability") or "").strip()
    step_id = step.get("id", "?")

    if not key:
        problems.append(f"step '{step_id}' has no capability.")
        return problems
    if key not in CAPABILITY_KEYS:
        problems.append(
            f"step '{step_id}' uses unknown capability '{key}'. "
            f"Known: {', '.join(sorted(CAPABILITY_KEYS))}."
        )
        return problems

    capability = get(key)
    inputs = binding.get("inputs") or {}
    if not isinstance(inputs, Mapping):
        problems.append(f"step '{step_id}' inputs must be an object.")
        inputs = {}

    for name in capability.required_inputs:
        value = inputs.get(name)
        # A reference to an earlier step counts as supplied — it is resolved at run time.
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(
                f"step '{step_id}' ({capability.label}) needs '{name}', which is not set."
            )

    unknown = set(inputs) - set(capability.required_inputs) - set(capability.optional_inputs)
    if unknown:
        # Named rather than ignored: a typo'd input that is silently dropped is a step that
        # does something subtly different from what the customer wrote down.
        problems.append(
            f"step '{step_id}' ({capability.label}) does not take: {', '.join(sorted(unknown))}."
        )

    problems.extend(validate_approval(step_id, capability, step.get("approval") or {}))
    return problems


#: What happens when nobody answers a gate in time.
#:
#: `approve` exists because real teams need it — a release manager on holiday should not stop
#: a low-risk change forever. It is recorded as a decision made BY POLICY, never as one made by
#: a person, which is the difference between an audit that passes and one that does not.
TIMEOUT_ACTIONS = ("block", "escalate", "approve")

#: A capability at or above this can never auto-approve on timeout, whatever the SLA says.
#: Deploying to production because nobody looked is not an approval.
TIMEOUT_APPROVE_RISK_CEILING = 2


def validate_approval(
    step_id: str, capability: "Capability", approval: Mapping[str, Any]
) -> list[str]:
    """Everything wrong with one step's approval policy."""
    problems: list[str] = []
    requested = approval.get("mode")
    if requested is not None and str(requested).lower() not in ("auto", "manual"):
        problems.append(f"step '{step_id}' approval mode must be 'auto' or 'manual'.")
    elif capability.risk_tier >= MANUAL_APPROVAL_RISK_FLOOR and str(requested).lower() == "auto":
        problems.append(
            f"step '{step_id}' ({capability.label}) is risk tier {capability.risk_tier} and "
            "always requires a person. Set approval to manual, or choose a different step."
        )

    sla = approval.get("sla_hours")
    if sla is not None:
        try:
            hours = float(sla)
        except (TypeError, ValueError):
            problems.append(f"step '{step_id}' sla_hours must be a number.")
        else:
            if hours <= 0 or hours > 24 * 30:
                problems.append(
                    f"step '{step_id}' sla_hours must be between 0 and 720 (30 days)."
                )

    on_timeout = approval.get("on_timeout")
    if on_timeout is not None:
        action = str(on_timeout).lower()
        if action not in TIMEOUT_ACTIONS:
            problems.append(
                f"step '{step_id}' on_timeout must be one of: {', '.join(TIMEOUT_ACTIONS)}."
            )
        elif action == "approve" and capability.risk_tier >= TIMEOUT_APPROVE_RISK_CEILING:
            # The floor again, from the other direction. A gate that approves itself because
            # nobody looked is not a gate, and this is the setting that would quietly turn one
            # off without ever saying "auto".
            problems.append(
                f"step '{step_id}' ({capability.label}) is risk tier {capability.risk_tier} "
                "and cannot approve itself on a timeout. Use 'escalate' or 'block'."
            )
        elif action != "block" and sla is None:
            problems.append(
                f"step '{step_id}' has on_timeout='{action}' but no sla_hours, so the timeout "
                "would never fire."
            )

    return problems


def effective_approval(
    capability_key: str, approval: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    """The approval policy this step will ACTUALLY run under, with its defaults filled in.

    Stored on the step so the plan says what will happen rather than what was asked for.
    """
    approval = dict(approval or {})
    capability = get(capability_key)
    resolved: dict[str, Any] = {
        "mode": approval_mode_for(capability_key, approval.get("mode")),
        # Defaults to blocking. A gate whose unanswered outcome is "proceed" unless somebody
        # configured otherwise is a gate that fails open, and this one guards production.
        "on_timeout": str(approval.get("on_timeout") or "block").lower(),
    }
    if approval.get("approver_role"):
        resolved["approver_role"] = str(approval["approver_role"])
    if approval.get("sla_hours") is not None:
        resolved["sla_hours"] = float(approval["sla_hours"])
    resolved["risk_tier"] = capability.risk_tier
    resolved["forced_manual"] = capability.risk_tier >= MANUAL_APPROVAL_RISK_FLOOR
    return resolved


def project_requirements(spec: Mapping[str, Any]) -> set[str]:
    """What a project must have for this whole workflow to be runnable.

    Collected at publish time so "you have not connected a repository" is said once, while the
    customer is looking at the workflow — not on the morning the build step finally runs.
    """
    needed: set[str] = set()
    for step in spec.get("steps") or []:
        if step.get("kind") != "capability":
            continue
        key = str((step.get("binding") or {}).get("capability") or "")
        if key in CAPABILITY_KEYS:
            needed.update(get(key).requires_project)
    return needed


def estimate_minutes(spec: Mapping[str, Any]) -> int:
    """Roughly how long this workflow takes, so a plan can be read before it is run.

    Sums the steps rather than modelling the graph: a parallel branch makes this an
    over-estimate, which is the safe direction for a number a customer plans around.
    """
    total = 0
    for step in spec.get("steps") or []:
        if step.get("kind") != "capability":
            continue
        binding = step.get("binding") or {}
        if binding.get("custom_step"):
            # A customer's step calls one tool; there is nothing to estimate from. Counted at a
            # flat few minutes rather than zero, because a workflow whose custom steps are free
            # reads as faster than it is.
            total += CUSTOM_STEP_MINUTES
            continue
        key = str(binding.get("capability") or "")
        if key in CAPABILITY_KEYS:
            total += get(key).typical_minutes
    return total


# ── Custom steps (Phase 6) ─────────────────────────────────────────────────────
#
# A step the customer defines, bound to a skill they registered rather than to one of the
# capabilities above. Deliberately last: it needs the capability registry to model against,
# the risk floor to sit under, and provenance on whatever they attached.
#
# It is NOT a way around the floor. A custom step inherits the risk tier of the skill it binds,
# and the same rule applies — at or above the floor it is a human decision. Otherwise "custom
# step" would be the documented method for deploying to production unattended.


def validate_custom_step(
    step: Mapping[str, Any], skill: Optional[Mapping[str, Any]]
) -> list[str]:
    """Everything wrong with one custom step, given the skill it binds.

    `skill` is what the project's registry knows about that skill id, or None if it has none —
    which is itself the most common problem and gets the clearest sentence.
    """
    problems: list[str] = []
    step_id = step.get("id", "?")
    binding = step.get("binding") or {}
    skill_id = str(binding.get("skill_id") or "").strip()

    if not skill_id:
        problems.append(f"step '{step_id}' is a custom step with no skill attached.")
        return problems
    if skill is None:
        problems.append(
            f"step '{step_id}' attaches skill '{skill_id}', which this project does not have. "
            "Register it first, or choose one of the standard steps."
        )
        return problems
    if not skill.get("enabled", True):
        # Fail closed. A disabled skill that silently no-ops is worse than a step that refuses
        # to publish: the workflow would appear to run and do nothing.
        problems.append(
            f"step '{step_id}' attaches skill '{skill_id}', which is disabled in this project."
        )

    risk = int(skill.get("risk_tier") or 0)
    approval = step.get("approval") or {}
    if risk >= MANUAL_APPROVAL_RISK_FLOOR and str(approval.get("mode", "")).lower() == "auto":
        problems.append(
            f"step '{step_id}' attaches skill '{skill_id}' at risk tier {risk} and always "
            "requires a person. Set approval to manual."
        )

    provenance = skill.get("provenance") or {}
    if not provenance.get("trusted", False) and risk >= MANUAL_APPROVAL_RISK_FLOOR:
        # Third-party supply chain. BOSS records provenance on imported toolchain sources for
        # exactly this moment: an untrusted skill that can change the world does not get to run
        # without somebody agreeing to it.
        problems.append(
            f"step '{step_id}' attaches an untrusted skill '{skill_id}' at risk tier {risk}. "
            "Only a reviewed skill can be given that much reach."
        )

    return problems


def custom_step_approval(skill: Mapping[str, Any], requested: Optional[str]) -> str:
    """The approval mode a custom step will run under, under the same floor."""
    risk = int((skill or {}).get("risk_tier") or 0)
    if risk >= MANUAL_APPROVAL_RISK_FLOOR:
        return "manual"
    mode = (requested or "auto").strip().lower()
    if mode not in ("auto", "manual"):
        raise ValueError("approval mode must be 'auto' or 'manual'.")
    return mode


# ── Control blocks ─────────────────────────────────────────────────────────────
#
# The runtime has supported these since WFL-001 and the builder never offered them, so a
# customer could only ever author a straight line. They are not capabilities — they do no work
# and call no tool — so they live in their own list rather than being bent into the shape of
# one.
#
# Only the three that mean something in a LINEAR canvas are offered. `condition`, `parallel`,
# `join` and `bounded_loop` need somewhere to draw a branch, and a palette entry that produces
# a spec the runtime rejects is worse than no palette entry: it is a promise the product
# cannot keep. They are listed as `canvas_required` so the builder can say they exist and are
# not yet placeable, rather than pretending they do not exist.

CONTROL_BLOCKS: list[dict[str, Any]] = [
    {
        "kind": "timer",
        "label": "Wait",
        "summary": "Pause for a fixed time — a grace period, a bake window, a cooling-off.",
        "canvas_required": False,
        "defaults": {"wait_seconds": 3600},
        "configure": [
            {"field": "wait_seconds", "label": "Wait for (seconds)", "type": "number"}
        ],
    },
    {
        "kind": "decision",
        "label": "Approval gate",
        "summary": "Stop and wait for a person, anywhere in the flow — not only where a "
                   "capability already implies one.",
        "canvas_required": False,
        "defaults": {},
        "configure": [
            {"field": "approver_role", "label": "Who approves", "type": "text"}
        ],
    },
    {
        "kind": "subworkflow",
        "label": "Run another workflow",
        "summary": "Call one of your workflows as a step, so a shared sequence lives in one "
                   "place instead of being copied into six.",
        "canvas_required": False,
        "defaults": {},
        "configure": [
            {"field": "workflow_version_id", "label": "Which workflow", "type": "workflow"}
        ],
    },
    {
        "kind": "condition",
        "label": "Branch",
        "summary": "Take a different path depending on what an earlier step produced.",
        "canvas_required": True,
    },
    {
        "kind": "parallel",
        "label": "Do at the same time",
        "summary": "Split into branches that run together — several reviews at once.",
        "canvas_required": True,
    },
    {
        "kind": "join",
        "label": "Wait for branches",
        "summary": "Continue when all (or any) of the parallel branches have finished.",
        "canvas_required": True,
    },
    {
        "kind": "bounded_loop",
        "label": "Repeat until",
        "summary": "Repeat a step until a condition holds, with a hard ceiling on attempts.",
        "canvas_required": True,
    },
]


def blocks() -> list[dict[str, Any]]:
    """Control blocks for the builder palette, placeable ones first."""
    return sorted(CONTROL_BLOCKS, key=lambda b: (b["canvas_required"], b["label"]))
