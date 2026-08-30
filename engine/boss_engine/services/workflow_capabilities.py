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
            optional_inputs=("task_id",),
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
            label="Build",
            summary="A governed code task: sandbox, tests, review seats, gated PR.",
            tool="create_code_task",
            risk_tier=1,
            required_inputs=("title", "instructions"),
            optional_inputs=("acceptance_criteria", "test_commands", "task_id", "repository"),
            produces=("code_task_run_id", "pull_request_url"),
            requires_project=("repository",),
            skills=("github.create_pr_real", "software_factory.sandbox_validation"),
            typical_minutes=45,
            is_async=True,
        ),
        _cap(
            key="review",
            label="Adversarial review",
            summary="Independent seats judge the change: correctness, security, consistency.",
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
            summary="BOSS re-runs the tests itself and keeps the output as evidence.",
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
            key="document",
            label="Document",
            summary="Publish the change record and the evidence behind it.",
            tool=None,
            risk_tier=1,
            required_inputs=("title",),
            optional_inputs=("markdown",),
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

    approval = step.get("approval") or {}
    requested = approval.get("mode")
    if requested and str(requested).lower() not in ("auto", "manual"):
        problems.append(f"step '{step_id}' approval mode must be 'auto' or 'manual'.")
    elif capability.risk_tier >= MANUAL_APPROVAL_RISK_FLOOR and str(requested).lower() == "auto":
        problems.append(
            f"step '{step_id}' ({capability.label}) is risk tier {capability.risk_tier} and "
            "always requires a person. Set approval to manual, or choose a different step."
        )

    return problems


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
        key = str((step.get("binding") or {}).get("capability") or "")
        if key in CAPABILITY_KEYS:
            total += get(key).typical_minutes
    return total
