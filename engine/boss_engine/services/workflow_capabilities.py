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
from typing import Any, Iterable, Mapping, Optional

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
    #: stays `waiting` until the thing it started reports back. Every async capability MUST
    #: have an await spec in the runner (`_AWAIT_KINDS`) — otherwise nothing waits and the step
    #: goes green on "accepted" — and `test_every_async_capability_is_awaited` enforces it.
    is_async: bool = False
    #: WHO ANSWERS FOR THIS STEP — a registered role, not a description.
    #:
    #: Every step offered "Ask BOSS about this step", which reaches BOSS-in-general: no lens, no
    #: remit, and no memory of anything this kind of work has produced before. The role-addressed
    #: path already exists and is strictly better — it reads the role's own definition and the
    #: positions that role has actually taken on this project — it simply had nothing telling it
    #: WHICH role a deploy or a security review belongs to.
    #:
    #: One captain may hold several steps; that is normal and says something true about the
    #: shape of the workforce. What is not acceptable is a step with no owner.
    captain: str = ""
    #: True when the step's work is done by a model, so a project with no model connected
    #: cannot run it. Read by `workflow_readiness` before a run starts.
    needs_model: bool = False
    #: WHO ANSWERS THE GATE. "person" for a real sign-off; "panel" for `spec_review`, which
    #: adopts the panel's own PRD decision — approved automatically by default
    #: (BOSS_MEETING_PRD_AUTO_APPROVE). Counting it as a person's approval made nine templates
    #: promise a human sign-off that, on boss-1, was granted by `auto_approval` twenty seconds
    #: after the panel ended (wfr_9c78…).
    answered_by: str = "person"
    #: What this step costs beyond its minutes, when that is the thing worth knowing before
    #: starting — PLRR is a second build pass, and nothing on the plan said so.
    cost_note: str = ""
    #: WHAT KIND OF WORK IT IS, for the one verb every agent surface says (agent_acts.work_verb):
    #: deploy (Deploying), publish (Publishing), monitor (Monitoring), research (Researching).
    #: Empty = no family, and the verb is plain "Working" — never guessed from the label.
    family: str = ""
    #: True when the step's own work ends in a PERSON's sign-off of what it produced (the
    #: architecture document). Not a gate before the step — the step runs, then waits for the
    #: answer — but a stop an operator plans around all the same, and no agent may give it.
    reviewed_by_a_person: bool = False
    #: THE TOOLS THE STEP CALLS IN-PROCESS, beyond `tool`. A capability with `tool=None` that
    #: starts work through a tool anyway (PLRR's and More tests' extend_code_task, a build with
    #: `extend`) declares it here: the consent a workflow asks for is derived from `tool` AND
    #: these (identity.consent.workflow_needs), and the runner refuses an in-process call to a
    #: tool its capability does not declare (workflow_capability_runner._start_on_the_branch).
    #: Undeclared, the consent never asked for code_task:retry and every More tests pass was
    #: refused under enforce (wfr_9575c86ab4a74defa0d2).
    calls: tuple[str, ...] = ()


def _cap(**kwargs: Any) -> Capability:
    return Capability(**kwargs)


#: The catalogue of what a step can be. Ordered as a delivery flow reads, because this is also
#: the order the builder palette shows them in.
CAPABILITIES: dict[str, Capability] = {
    c.key: c
    for c in (
        _cap(
            key="research",
            family="research",
            # is this worth building, and for whom
            captain="product_manager",
            label="Research",
            summary="Web research into a cited dossier, gated on real evidence.",
            tool="create_discovery",
            risk_tier=1,
            required_inputs=("seed",),
            optional_inputs=("title", "context", "deep"),
            produces=("discovery_id", "dossier"),
            typical_minutes=15,
            is_async=True,
            needs_model=True,
        ),
        _cap(
            key="meeting",
            # owns the PRD the panel produces
            captain="product_manager",
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
            needs_model=True,
        ),
        _cap(
            key="spec_review",
            # is this specified well enough to build
            captain="chief_architect",
            # NAMED FOR WHAT HAPPENS. This was "Approve the spec — a person reads the PRD and
            # decides whether it gets built". It adopts the panel's own PRD decision, and that
            # decision is approved automatically unless BOSS_MEETING_PRD_AUTO_APPROVE is turned
            # off: on boss-1 the step recorded `approved_by: auto_approval` twenty seconds after
            # the panel (wfr_9c78…). The owner kept it automatic, so the label stops promising
            # a sign-off nobody gives.
            label="PRD approval (automatic)",
            summary="The panel's PRD is approved before anything is built: by a person with the "
                    "ceo seat on the Approvals page when identity is enforced, automatically "
                    "when BOSS_MEETING_PRD_AUTO_APPROVE is on (the default without enforcement).",
            tool=None,
            risk_tier=2,
            required_inputs=(),
            optional_inputs=("approver_role",),
            produces=("decision_id",),
            is_gate=True,
            answered_by="panel",
        ),
        _cap(
            key="architecture",
            # how it is put together, decided before anybody writes code
            captain="chief_architect",
            label="Architecture approval",
            summary="The architect turns the approved PRD into an architecture document "
                    "(components, data model, APIs, security, deployment, risks, decisions). "
                    "A person approves it, asks for changes, or rejects it; builds receive "
                    "the approved version.",
            # No tool: the runner writes the document itself (services/architecture_docs.py)
            # and the step WAITS on the person's answer — the approval lives inside the step,
            # with the revision loop, so it is one block on the canvas, not two.
            tool=None,
            # NOT the risk floor: that would put a "may it run?" gate in front of the
            # architect. The person's sign-off is the step's own, after the document exists,
            # and `stops_for_a_person` says so through `reviewed_by_a_person`.
            risk_tier=1,
            optional_inputs=("constraints",),
            produces=("architecture_review_id", "architecture_doc_id", "architecture_version",
                      "architecture_markdown", "decision_id"),
            typical_minutes=10,
            is_async=True,
            needs_model=True,
            reviewed_by_a_person=True,
        ),
        _cap(
            key="build",
            # owns the inner pipeline end to end
            captain="engineering_captain",
            label="Build, review and test",
            summary="The whole inner pipeline: implement, adversarial review, security check, "
                    "run the tests, open a gated pull request. Succeeds when a PR exists.",
            tool="create_code_task",
            # `extend` (Incremental Build) builds on the run's branch: extend_code_task.
            calls=("extend_code_task",),
            risk_tier=1,
            required_inputs=("title", "instructions"),
            # spec_markdown is the PRD the panel wrote. Optional, because a build reached
            # directly from a one-line directive has no panel behind it — but when there IS
            # one, this is the whole point of having held it.
            # `extend`: build on THIS run's branch, from its newest finished pass, as more
            # commits on the same PR (Incremental Build's later increments). `increment`: which
            # increment of the PRD's plan this build builds (workflow_catalogue.increment_brief).
            optional_inputs=("acceptance_criteria", "test_commands", "task_id", "repository",
                             "reference_links", "spec_markdown", "extend", "increment"),
            produces=("code_task_run_id", "pull_request_url"),
            requires_project=("repository",),
            skills=("github.create_pr_real", "software_factory.sandbox_validation"),
            typical_minutes=45,
            is_async=True,
            needs_model=True,
            cost_note="A full build: implement, review, test and open a pull request — "
                      "usually the largest model spend in the workflow.",
        ),
        _cap(
            key="review",
            # argues the code is wrong until it is not
            captain="adversarial_reviewer",
            label="Adversarial review",
            summary="Reports what the build's own reviewers found. The verdict belongs to the "
                    "build step — this only surfaces it.",
            tool=None,
            risk_tier=1,
            optional_inputs=("seats",),
            produces=("verdicts",),
            typical_minutes=10,
            # NOT async. It reads a verdict the build's pipeline has already written down, in
            # the call, and waits on nothing. Declaring it async was a claim nothing could
            # honour: "async" means the runner parks the step against an await table, and a
            # capability marked async with none is how deploy went green on "planned".
            is_async=False,
        ),
        _cap(
            key="test",
            # owns whether the tests prove anything
            captain="qa",
            label="Verify",
            summary="Reports the build's own test outcome. The verdict belongs to the build "
                    "step — this only surfaces it.",
            tool=None,
            risk_tier=0,
            optional_inputs=("commands",),
            produces=("tests_passed", "evidence"),
            typical_minutes=5,
            # NOT async. It reads a verdict the build's pipeline has already written down, in
            # the call, and waits on nothing. Declaring it async was a claim nothing could
            # honour: "async" means the runner parks the step against an await table, and a
            # capability marked async with none is how deploy went green on "planned".
            is_async=False,
        ),
        _cap(
            key="report",
            # answers for what the run cost and who spent it
            captain="program_manager",
            label="Run report",
            summary="What every step so far cost, which agent did it, and what went wrong — "
                    "as a document that can be downloaded.",
            tool=None,
            risk_tier=0,
            required_inputs=(),
            produces=("artifact_id", "total_usd", "blockers"),
            typical_minutes=1,
        ),
        _cap(
            key="preview",
            family="deploy",
            # a person has to be able to use it — started by the role that built it, whose
            # Passport (Builder) holds preview:manage; QA's (Reviewer) does not.
            captain="engineering_captain",
            label="Preview",
            summary="Start a live preview of the app so a person can use it.",
            tool="start_preview",
            risk_tier=1,
            required_inputs=("run_id",),
            # `advisory`: a preview BOSS added after pass 1 (two_pass). If the app cannot be
            # previewed it says so and the run goes on.
            optional_inputs=("ttl_minutes", "advisory"),
            produces=("preview_id", "preview_url"),
            typical_minutes=5,
            is_async=True,
        ),
        _cap(
            key="ux_review",
            # how it looks and feels is the whole step
            captain="ux_engineer",
            label="UX sign-off",
            summary="A person uses the preview and signs off on how it looks and behaves.",
            tool=None,
            risk_tier=2,
            optional_inputs=("approver_role",),
            produces=("decision_id",),
            is_gate=True,
        ),
        _cap(
            key="release_approval",
            # whether this change may ship now
            captain="release_manager",
            label="Approve the release",
            summary="A person reads what is about to ship (the change, its checks, why it is "
                    "needed) and approves it, whatever the environment's deploy policy would "
                    "allow without asking. For releases a person must own, like an incident fix.",
            tool=None,
            risk_tier=2,
            optional_inputs=("approver_role",),
            produces=("decision_id",),
            is_gate=True,
        ),
        _cap(
            key="security_review",
            # the lens the step exists for
            captain="security_reviewer",
            label="Security sign-off",
            summary="A person accepts the security posture of the change.",
            tool=None,
            risk_tier=3,
            optional_inputs=("approver_role",),
            produces=("decision_id",),
            is_gate=True,
        ),
        # ── THE SECURITY REVIEW (spec §4.2; services/security_review_steps.py) ──────────────
        # scan -> panel -> decide -> fix -> rescan -> rereview (x2 rounds) -> report. Real tools
        # find; a Panel recommends; a person chooses; a build fixes; the same tools re-scan; the
        # re-review passes only when every targeted finding is gone and no new high appeared.
        _cap(
            key="security_scan",
            captain="security_reviewer",
            label="Security scan",
            summary="Scan the app with Semgrep, OSV-Scanner and ZAP (in its own sandbox). On a "
                    "re-scan, the same tools and rules as the first.",
            tool="run_security_scan",
            risk_tier=1,
            required_inputs=("app_key",),
            optional_inputs=("tools", "zap_mode", "after"),
            produces=("scan_id", "counts", "commit_sha"),
            typical_minutes=10,
            is_async=True,
        ),
        _cap(
            key="security_panel",
            captain="security_reviewer",
            label="Security panel",
            summary="A panel chaired by the security lens turns the findings into a short, "
                    "prioritised list of fixes. It may call a finding a false positive; it "
                    "cannot remove one.",
            tool="request_meeting",
            risk_tier=1,
            optional_inputs=("app_key", "focus"),
            produces=("meeting_run_id", "prd_markdown"),
            typical_minutes=15,
            is_async=True,
            needs_model=True,
        ),
        _cap(
            key="security_decide",
            captain="security_reviewer",
            label="Choose the fixes",
            summary="A person ticks which recommendations to fix (P0 and P1 start ticked) and may "
                    "accept the rest with a reason.",
            tool=None,
            risk_tier=2,
            optional_inputs=("approver_role",),
            produces=("decision_id",),
            is_gate=True,
        ),
        _cap(
            key="security_fix",
            captain="engineering_captain",
            label="Build the fixes",
            summary="The app's next build: the chosen fixes, with the findings each must make "
                    "disappear. Round 2 gets exactly what the re-scan still found.",
            tool="create_code_task",
            risk_tier=1,
            required_inputs=("app_key",),
            optional_inputs=("round", "after_review"),
            produces=("code_task_run_id", "pull_request_url"),
            typical_minutes=45,
            is_async=True,
            needs_model=True,
            cost_note="A build of the app: implement, review, test. Runs a second time only "
                      "when the first fix leaves a targeted finding behind.",
        ),
        _cap(
            key="security_rereview",
            captain="security_reviewer",
            label="Security re-review",
            summary="Passes only when every targeted finding is gone from the re-scan (or "
                    "justified by a person) and no new high or critical finding appeared.",
            tool=None,
            risk_tier=1,
            optional_inputs=("app_key", "after", "round", "final"),
            produces=("passed", "summary", "after_scan_id"),
            typical_minutes=1,
        ),
        _cap(
            key="security_report",
            captain="security_reviewer",
            label="Security report",
            summary="The before/after: what was fixed, what remains, what is new, with tool "
                    "versions and commits. Shown in the Factory; downloadable as Markdown.",
            tool=None,
            risk_tier=0,
            optional_inputs=("app_key",),
            produces=("artifact_id", "passed", "summary"),
            typical_minutes=1,
        ),
        _cap(
            key="plrr",
            # readiness to go to production
            captain="release_manager",
            label="Production launch readiness",
            summary="Review the built app against the operability the spec promised, and "
                    "close the gaps before it ships.",
            tool=None,
            calls=("extend_code_task",),
            risk_tier=1,
            required_inputs=("run_id",),
            optional_inputs=("directive",),
            produces=("findings", "gaps_closed", "code_task_run_id"),
            # A SECOND BUILD PASS, AND THE PLAN NOW SAYS SO. PLRR extends the build into a new
            # code task on the same branch to close the operability gaps it finds, so it costs
            # about what a build costs — and the plan said "12 minutes" and nothing about money.
            # An operator choosing Standard Product Delivery was never told it builds twice.
            typical_minutes=30,
            is_async=True,
            needs_model=True,
            cost_note="A second build pass on the same branch: it checks the app against the "
                      "operability the spec promised and writes what is missing. Expect "
                      "build-level model spend and time.",
        ),
        _cap(
            key="more_tests",
            # pass 2 of a build: coverage after the app already works
            captain="qa",
            label="More tests",
            summary="After the app works and its preview is up, add tests for what was built "
                    "(edge cases, error paths, API contracts, browser tests) on the same PR, and "
                    "fix any bug they find. Never holds up the build, its PR or its preview.",
            tool=None,
            calls=("extend_code_task",),
            risk_tier=1,
            required_inputs=("run_id",),
            # "light" (two_pass.LIGHT): a hotfix's pass 2, regression tests around the change.
            optional_inputs=("depth",),
            produces=("code_task_run_id", "bugs_fixed"),
            typical_minutes=15,
            is_async=True,
            needs_model=True,
            cost_note="A second, test-focused pass on the same branch and PR. If it fails, the "
                      "run carries on with the first pass's code.",
        ),
        _cap(
            key="deploy",
            family="deploy",
            # gets it onto a machine and keeps it there
            captain="deployment_manager",
            label="Deploy",
            summary="Ship the built app. Its first deploy creates its target on the chosen machine.",
            tool="deploy_app",
            risk_tier=2,
            required_inputs=("run_id",),
            # WHERE is the only question (host_id: a Fleet machine, or blank = the project's one
            # verified host / ask at deploy time). The target is created at the first deploy.
            # `pass_2_of`: "Deploy again" — ship the More tests pass where that deploy step
            # shipped pass 1, as a new deploy under the same policy (two_pass).
            # `monitoring_step`: the Set up monitoring step whose pack this release carries
            # (monitoring_pack); under `monitoring_gate: block` it cannot ship without one.
            optional_inputs=("environment", "target_id", "host_id", "pass_2_of",
                             "monitoring_step"),
            produces=("deployment_id", "deployed_url"),
            requires_project=("deployment_target",),
            typical_minutes=8,
            is_async=True,
        ),
        _cap(
            key="video_edit",
            family="publish",
            # OWNS WHAT THE FINISHED FILE LOOKS LIKE — the cut, the branding, the bookends.
            # Separate from the role that PUBLISHES it, so the agent that can put something in
            # front of an audience is not the one that assembled it.
            captain="video_editor",
            label="Edit the video",
            summary="Trim, brand and bookend a video, leaving the source untouched.",
            # No built-in tool: which edit happens is decided by the SKILL the step binds to
            # (video.trim, video.watermark, video.bookend), which is what lets one capability
            # serve a three-step edit chain without three capabilities.
            tool=None,
            # Writes a new file and destroys nothing. `drive.file` cannot reach a file BOSS did
            # not create, so the operator's source is safe by permission rather than by policy.
            risk_tier=1,
            required_inputs=("media_artifact_id",),
            optional_inputs=("start", "end", "logo_artifact_id", "corner", "scale", "margin",
                             "intro_artifact_id", "outro_artifact_id", "output_name",
                             "output_folder_id"),
            produces=("media_artifact_id", "view_url", "duration_seconds"),
            typical_minutes=6,
        ),
        _cap(
            key="social_publish",
            family="publish",
            # OWNS THE ACCOUNT AND ANSWERS FOR WHAT GOES OUT ON IT.
            #
            # One capability, one platform per STEP. A single step that published everywhere
            # would produce one receipt for several effects, and a partial failure — YouTube
            # accepted, Instagram rate-limited — leaves a run in a state the approval gate
            # cannot describe. One effect, one receipt, one dedupe key is the whole governance
            # model; fanning out two steps in parallel costs nothing and stays retryable
            # independently.
            #
            # WHICH platform is decided by the skill the step is bound to
            # (`publish_youtube`, `publish_instagram`), not by this shape. That is what lets a
            # second platform be a new agent and a new binding rather than a new capability.
            captain="media_publisher",
            label="Publish to a channel",
            summary="Publish finished media to a social channel the assigned agent owns.",
            # No built-in tool: there is no generic "post to the internet". A step of this kind
            # is always bound to a skill that holds one account's credential, which is exactly
            # what a custom step binding is for.
            tool=None,
            # IRREVERSIBLE AND PUBLIC. You cannot un-publish something people have already
            # seen, so this sits at the tier that forces a person to agree first.
            risk_tier=3,
            # `media_artifact_id`, NOT `media`. The name has to match what an editing step
            # PRODUCES, or the chain cannot be wired: a spec builder connects an input to the
            # nearest upstream step producing a field of the same name, and `media` vs
            # `media_artifact_id` meant the publish step read from the run's input instead of
            # from the video that was just edited. The skill's own input_schema already called
            # it media_artifact_id; this was the odd one out.
            required_inputs=("media_artifact_id", "title"),
            optional_inputs=("description", "tags", "visibility", "thumbnail", "schedule_at",
                             "made_for_kids"),
            produces=("post_url", "post_id"),
            typical_minutes=5,
            # NOT async AS A BARE CAPABILITY: with no tool behind it there is nothing to wait on.
            # The publishing itself is done by the skill or custom step the workflow binds, and
            # that path declares its own waiting.
            is_async=False,
        ),
        _cap(
            key="monitoring_pack",
            family="monitor",
            # THE SRE WRITES THE MONITORING PACK BEFORE THE RELEASE (monitoring_pack.py).
            captain="sre",
            label="Set up monitoring",
            summary="Read the app's code, its series on its first environment and the pull "
                    "request, then write SEV-rated PromQL rules, a runbook per rule and "
                    "dashboard panels into boss.deploy.yaml on the same branch, previewed "
                    "against the last hour. The release is approved with the pack.",
            tool=None,
            risk_tier=1,
            required_inputs=("run_id",),
            optional_inputs=("first_deploy",),
            produces=("pack", "commit_sha"),
            typical_minutes=3,
            needs_model=True,
        ),
        _cap(
            key="monitor",
            family="monitor",
            # owns whether anyone would notice it break
            captain="infra_steward",
            # "Set up monitoring" is the pack before the release (monitoring_pack); this step
            # confirms BOSS is probing the app once it is live.
            label="Watch the app",
            summary="Register health checks so the app is watched after it ships.",
            tool=None,
            risk_tier=1,
            optional_inputs=("interval_minutes", "thresholds"),
            produces=("monitor_id",),
            typical_minutes=3,
        ),
        # ── INCIDENT RESPONSE (incident_response.py) ─────────────────────────────────────
        # Three steps around the ordinary fix build: what happened, did the fix hold, and the
        # record. None needs a model — the investigation correlates evidence any operator could
        # read; the fix itself is a normal governed build.
        _cap(
            key="investigate_incident",
            family="monitor",
            # owns what the monitor saw and what it means
            captain="sre",
            label="Investigate the incident",
            summary="Correlate the app's metrics, its logs and the serving release's diff into "
                    "a root-cause hypothesis and the brief the fix is built from.",
            tool=None,
            risk_tier=0,
            required_inputs=("incident_id",),
            produces=("incident_brief", "title", "root_cause", "suspect_commit",
                      "extends_run_id"),
            typical_minutes=1,
        ),
        _cap(
            key="verify_recovery",
            family="monitor",
            captain="sre",
            label="Verify recovery",
            summary="Wait until BOSS's own monitor shows the app's error rate back at its "
                    "pre-incident baseline on real traffic, for a stability window. Fails on "
                    "timeout. Never takes the fixing agent's word for it.",
            tool=None,
            risk_tier=0,
            required_inputs=("incident_id",),
            optional_inputs=("stable_minutes", "timeout_minutes"),
            produces=("recovery_check_id", "recovery_status", "recovery_summary"),
            typical_minutes=5,
            is_async=True,
        ),
        _cap(
            key="incident_report",
            family="monitor",
            captain="sre",
            label="Write the incident report",
            summary="The timeline, the root cause and what this response cost (billed and "
                    "subscription-included kept apart), exportable as Markdown and JSON; tells "
                    "the apps that reported it.",
            tool=None,
            risk_tier=1,
            required_inputs=("incident_id",),
            produces=("report_markdown", "minutes_to_recover", "cost_billed_usd"),
            typical_minutes=1,
        ),
        _cap(
            key="notify",
            # who needs telling, and what they are told
            captain="program_manager",
            label="Notify",
            # A REAL CHANNEL, OR A VISIBLE SKIP. This had no tool, returned `recorded` and went
            # green: Standard Product Delivery, Minor Feature and Fast Fix ended by telling
            # nobody, and Deprecation / Sunset "announced" a removal to nobody and then waited
            # seven days. The runner now sends it (`_notify`) and says when it could not.
            summary="Tell people the outcome: an email address (email:someone@example.com) or "
                    "a Mattermost channel (mattermost:<id>). With no channel the step is "
                    "skipped and says so; a channel that cannot be reached fails the step.",
            tool=None,
            risk_tier=1,
            required_inputs=("message",),
            optional_inputs=("channel", "subject"),
            produces=("posted", "channel", "receipt"),
            skills=("mattermost.post_real", "email.send_real"),
            typical_minutes=1,
        ),
        _cap(
            key="smoke_test",
            # does the shipped thing actually answer
            captain="integration_validator",
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
            key="wait_for_ci",
            # whether the pull request's own CI agrees with BOSS's gate
            captain="release_manager",
            label="Wait for CI",
            summary="Wait for the pull request's GitHub checks to finish. Passed lets the run go "
                    "on; a failed check or a timeout fails the step, links the check, and blocks "
                    "the deploy approval.",
            tool=None,
            risk_tier=0,
            optional_inputs=("pull_request_url", "head_sha", "timeout_minutes"),
            produces=("ci_wait_id", "ci_status", "ci_summary", "failing_check_url"),
            requires_project=("repository",),
            typical_minutes=15,
            is_async=True,
        ),
        _cap(
            key="redeploy",
            family="deploy",
            # same hands as the first deploy
            captain="deployment_manager",
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
            # same hands as the build
            captain="engineering_captain",
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
            family="deploy",
            # reclaims what the preview was holding
            captain="infra_steward",
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
            # what it costs is its only question
            captain="finops_sentinel",
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
            # owns the written record of the run
            captain="program_manager",
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
        # ── BUSINESS RULES (services/knowledge/rule_scan.py, KNOWLEDGE_GRAPH_PRODUCT_SPEC §4.5) ──
        # An agent reads an area or package of the code graph and PROPOSES the rules it
        # enforces, with evidence; a person approves each one. Nothing it writes is trusted
        # until then (rules.propose_rule writes `inferred`; only a person sets `approved`).
        _cap(
            key="scan_business_rules",
            family="research",
            # reads the system's structure and says what it enforces
            captain="chief_architect",
            label="Scan for business rules",
            summary="Read an area or package of the code, class by class, and propose the "
                    "business rules it enforces, each with the lines that prove it. Stops at "
                    "the files, rules or budget limit, whichever comes first.",
            # No tool: the runner runs the scan in-process (rule_scan.run_step). It reads code
            # through repo.read's read_code, so that is the tool it declares it calls.
            tool=None,
            calls=("read_code",),
            # Spends model budget and writes inferred rules, which a person can reject.
            risk_tier=1,
            # `scope`: area or package keys from the Graph's project tree. `budget_usd`: the
            # scan's own ceiling (default 2.00, at most the project's cap).
            required_inputs=("scope",),
            # `focus`: what the person wants read for first, in their words (the run's brief).
            optional_inputs=("focus", "max_files", "max_rules", "budget_usd"),
            produces=("scan_id", "files_read", "rules_proposed", "spend_usd", "stopped_by",
                      "report", "review_link"),
            requires_project=("repository",),
            skills=("repo.read", "knowledge.propose"),
            typical_minutes=20,
            needs_model=True,
            cost_note="Model spend up to the scan's budget (default $2.00), charged to the "
                      "knowledge budget.",
        ),
        _cap(
            key="rules_review",
            # answers for the review; a PERSON approves each rule (Rules review allows only
            # people who hold knowledge:review)
            captain="chief_architect",
            label="Review proposed rules",
            summary="A person approves, edits or rejects each rule the scan proposed, in "
                    "Graph › Rules review. Ends when the scan's queue is empty or a person "
                    "closes the review.",
            tool=None,
            risk_tier=2,
            optional_inputs=("scan_id", "approver_role"),
            produces=("decision_id",),
            is_gate=True,
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
            # Who answers for this step. Travels with the capability so the console does not
            # keep a second copy of the mapping to drift from this one.
            "captain": c.captain,
            # What the builder must show as fixed rather than as a choice.
            "approval_forced_manual": c.risk_tier >= MANUAL_APPROVAL_RISK_FLOOR,
            # Whether the run actually waits for a PERSON here — false for the PRD approval,
            # which the panel's decision answers (automatically by default).
            "stops_for_a_person": stops_for_a_person(c.key),
            "answered_by": c.answered_by,
            "needs_model": c.needs_model,
            "cost_note": c.cost_note,
            "reviewed_by_a_person": c.reviewed_by_a_person,
        }
        for c in CAPABILITIES.values()
    ]


def prd_auto_approved(settings: Any = None) -> bool:
    """Whether a panel's PRD is approved by BOSS (True) or waits for a person (False).

    THE runtime rule, read where the runtime reads it: meeting_service._finalize_meeting
    approves the PRD decision itself when `meeting_prd_auto_approve`
    (BOSS_MEETING_PRD_AUTO_APPROVE; unset, on only when identity is not enforced) is set. Everything that tells an operator
    whether they will be asked — the catalogue card, the plan, /asks — reads this function.
    """
    if settings is None:
        try:
            from ..config import get_settings

            settings = get_settings()
        except Exception:  # noqa: BLE001 — the setting's own default
            return True
    return bool(getattr(settings, "meeting_prd_auto_approve", True))


def stops_for_a_person(capability_key: str, requested: Optional[str] = None,
                       prd_auto_approve: Optional[bool] = None) -> bool:
    """Whether a run waits for a PERSON at this step — the question an operator plans around.

    Not the same as "approval is manual". `spec_review` is manual by the risk floor and is
    answered by the panel's own PRD decision, approved automatically by default, so counting it
    told the catalogue card and the plan "you approve: spec_review" for a step nobody is asked
    about. One function, so the card, the plan and the start preflight agree.

    "By default" is a setting, not a fact: with BOSS_MEETING_PRD_AUTO_APPROVE off a person
    signs the PRD, and this says so (`prd_auto_approve`, read from settings when not given).
    """
    capability = get(capability_key)
    if capability.reviewed_by_a_person:
        return True
    if capability.answered_by == "panel":
        auto = prd_auto_approved() if prd_auto_approve is None else bool(prd_auto_approve)
        return not auto
    if capability.answered_by != "person":
        return False
    return approval_mode_for(capability_key, requested) == "manual"


#: Where a notify step may post. The prefix is the channel kind, and each kind is a need the
#: start preflight checks (an SMTP server, a Mattermost bot).
NOTIFY_CHANNEL_KINDS = {"email": "email", "mattermost": "mattermost"}


def notify_channel_need(channel: str) -> str:
    """The readiness need a notify channel implies: `email`, `mattermost`, or `channel:<value>`
    for something that is not a channel at all — reported, never guessed at."""
    value = str(channel or "").strip()
    kind = value.split(":", 1)[0].lower()
    if ":" in value and kind in NOTIFY_CHANNEL_KINDS:
        return NOTIFY_CHANNEL_KINDS[kind]
    return f"channel:{value}"


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


#: The roles BOSS's own capabilities name as their DEFAULT captain — the vocabulary the palette
#: is written in, checked by test_every_step_has_a_captain.
#:
#: NOT WHO A STEP MAY NAME. That used to be this set, so an agent made in Agent Studio could
#: never be put on a step ("names captain 'docs_writer', which is not a role this workforce
#: has") while roles no project has were offered in the picker. Who a step may name is the
#: PROJECT's agents — the `roles` table — and `step_agents.assignment_problems` checks that at
#: publish and at start, where there is a project to ask.
REGISTERED_ROLES = frozenset({
    "adversarial_reviewer", "cao", "chief_architect", "coo", "cto", "deployment_manager",
    "design_critic", "engineering_captain", "finops_sentinel", "infra_steward",
    "integration_validator", "metatron", "model_spend_analyst", "pm", "product_manager",
    "program_manager", "providerops_manager", "qa", "release_manager", "resource_janitor",
    "secrets_steward", "security_reviewer", "software_engineer", "ux_engineer", "vp_agents",
    "vp_product",
    # OWNS THE CHANNELS AND WHAT GOES OUT ON THEM. The captain for `social_publish`, and the
    # role a per-platform publishing agent reports to: a channel credential belongs to somebody
    # whose remit is the audience, not to whichever step happened to need it.
    "media_publisher",
    # OWNS WHAT THE FINISHED FILE LOOKS LIKE. Separate from the publisher on purpose: the agent
    # that can put something in front of an audience should not be the one that assembled it,
    # and an editing mistake should be catchable by somebody who did not make it.
    "video_editor",
    # THE AGENT WORLD CAST (agent-world spec §0.1): the engineers a build is staffed with and
    # the two who keep it running. Installed by core_v1/core_v2 with a voice, skills and MCP.
    "backend_engineer", "frontend_engineer", "operations_engineer", "sre", "docs_writer",
    # A PERSON. Not an agent BOSS runs — the one participant it cannot.
    #
    # A gate exists because somebody wants a human to look. With no way to SAY that, the only
    # control on an approval step was a list of AI roles, so naming who approves meant naming a
    # machine — on the one step whose entire purpose is that a machine does not decide it.
    #
    # It is in this set so the builder offers it and `validate_captain` accepts it. Nothing
    # dispatches work to it: a step whose captain is `human` waits for a person, which is what
    # a gate already does.
    "human",
})

#: The roles that are people rather than agents. Small on purpose — this is not a category to
#: grow, it is the boundary between what BOSS runs and what it asks somebody to do.
HUMAN_ROLES = frozenset({"human"})


def validate_captain(step: Mapping[str, Any],
                     roster: Optional[Iterable[str]] = None) -> list[str]:
    """Whether a step's named agent is somebody this PROJECT has, and may be put there.

    First the SHAPE (steps stream): `human` only on a gate, a name that is an agent key.

    Then, when `roster` is given, EXISTENCE (agents stream): `roster` is everything the
    PROJECT's steps may name (workflows.project_roster: its agents plus the bundled roles it
    has not deleted). REGISTERED_ROLES alone is the bundled workforce, so an agent an operator
    made with POST /v1/agents — whose quick-create screen says it can be put on a step — was
    refused here as "not a role this workforce has". Every caller with the project in hand
    passes its roster (tests/test_a_created_agent_can_captain_a_step.py enforces it), so the
    fixed bundled list applies only where there is no project — the catalogue, which names only
    bundled roles. `step_agents.assignment_problems` checks existence again at publish and start.
    """
    from .step_agents import HUMAN, captain_shape_problems, named_agent

    named = named_agent(dict(step))
    if not named:
        return []
    problems = captain_shape_problems(dict(step))
    if problems or named == HUMAN:
        return problems
    # THE ROSTER IS THE WHOLE ANSWER WHEN THERE IS ONE. It already holds the bundled roles the
    # project has not deleted (workflows.project_roster); unioning REGISTERED_ROLES back in here
    # let a step name a bundled agent the operator had deleted (review of milestone/agents,
    # round 2), and the refusal that says what to do never fired.
    known = (set(REGISTERED_ROLES) if roster is None
             else {str(r).strip().lower() for r in roster} | HUMAN_ROLES)
    if named in known:
        return []
    others = sorted(known - HUMAN_ROLES)
    return [
        f"step '{step.get('id', '?')}' names agent '{named}', which this project does not "
        f"have: it is not a role this workforce has. Its agents include: "
        f"{', '.join(others[:6]) or 'none yet'}{', …' if len(others) > 6 else ''}. Pick one of "
        f"them, create '{named}' in Agent Studio first, or leave the captain empty for BOSS "
        "to pick."
    ]


def validate_step(step: Mapping[str, Any],
                  roster: Optional[Iterable[str]] = None) -> list[str]:
    """Everything wrong with one capability step, as sentences an operator can act on.

    Returns a list rather than raising on the first problem: a builder showing one error at a
    time makes a customer fix a workflow five times to publish it once.
    """
    return [message for _, message in validate_step_fields(step, roster)]


def validate_step_fields(step: Mapping[str, Any],
                         roster: Optional[Iterable[str]] = None) -> list[tuple[str, str]]:
    """`validate_step` with the field each sentence is about: `captain`, `binding`, `inputs`,
    `approval`, or the name of the input that is not set.

    The workflow editor puts each problem on the control that caused it, so the field is
    decided here, where the problem is found, and never parsed back out of the sentence.
    """
    problems: list[tuple[str, str]] = [("captain", m) for m in validate_captain(step, roster)]
    binding = step.get("binding") or {}
    key = str(binding.get("capability") or "").strip()
    step_id = step.get("id", "?")

    if not key:
        problems.append(("binding", f"step '{step_id}' has no capability."))
        return problems
    if key not in CAPABILITY_KEYS:
        problems.append((
            "binding",
            f"step '{step_id}' uses unknown capability '{key}'. "
            f"Known: {', '.join(sorted(CAPABILITY_KEYS))}."
        ))
        return problems

    capability = get(key)
    inputs = binding.get("inputs") or {}
    if not isinstance(inputs, Mapping):
        problems.append(("inputs", f"step '{step_id}' inputs must be an object."))
        inputs = {}

    for name in capability.required_inputs:
        value = inputs.get(name)
        # A reference to an earlier step counts as supplied — it is resolved at run time.
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append((
                name,
                f"step '{step_id}' ({capability.label}) needs '{name}', which is not set."
            ))

    unknown = set(inputs) - set(capability.required_inputs) - set(capability.optional_inputs)
    if unknown:
        # Named rather than ignored: a typo'd input that is silently dropped is a step that
        # does something subtly different from what the customer wrote down.
        problems.append((
            "inputs",
            f"step '{step_id}' ({capability.label}) does not take: {', '.join(sorted(unknown))}."
        ))

    problems.extend(("approval", m) for m in validate_approval(
        step_id, capability, step.get("approval") or {}))
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
    # A DEPLOY THAT LETS THE ENVIRONMENT'S POLICY ANSWER says so, and keeps saying so: without
    # it the step promises a person, and no policy answers for it (deploy_policy).
    if capability_key == "deploy" and approval.get("follows_policy") is True:
        resolved["follows_policy"] = True
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
    elif skill.get("requires_person") and str(approval.get("mode", "")).lower() == "auto":
        # At any tier: the skill calls a tool on an MCP server nobody has reviewed
        # (mcp_registry.needs_person). Round-2 review found this road running unattended.
        problems.append(
            f"step '{step_id}' attaches skill '{skill_id}', which requires a person: "
            f"{skill['requires_person']} Set approval to manual, or review the server first."
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
    if risk >= MANUAL_APPROVAL_RISK_FLOOR or (skill or {}).get("requires_person"):
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
# cannot keep. They are listed as `canvas_required`: only a builder that draws branches may
# place them. The console's Workflow Studio does — it places a condition, a parallel with its
# join, and a loop with its one body as whole blocks (apps/web/lib/workflow-blocks.ts), shaped
# so `normalise_workflow_spec` accepts them. A `join` is never placed alone.

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
        # A HUMAN APPROVES A GATE. That is what a gate IS, and leaving `approver_role` blank
        # for the operator to fill in invited them to type an AI agent's name into the one
        # control on the step whose whole purpose is that a person looks at it.
        "defaults": {"approver_role": "human"},
        # THE SAVE USED TO FAIL HERE AND THE BUILDER COULD NOT SEE WHY.
        #
        # `_validate_spec` requires `question` on a decision step; this list offered only
        # `approver_role`. So the builder collected no question, the save was refused, and the
        # operator reported the step as simply not working. A field the validator demands has
        # to be a field the palette offers.
        #
        # `guidance` is the other half, and it is what makes an approval answerable: the
        # reviewer of a video needs the link and what to look for, and the reviewer of a PRD
        # needs something else entirely. Rather than invent a taxonomy of approval TYPES, the
        # step carries the sentence its author would have written anyway, and the gate composes
        # it with whatever the earlier steps produced.
        "configure": [
            {"field": "question", "label": "What is being approved?", "type": "text"},
            {"field": "guidance", "label": "What should the reviewer check?", "type": "text"},
            {"field": "approver_role", "label": "Who approves", "type": "text"},
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
    {
        "kind": "for_each",
        "label": "For each",
        "summary": "Run a step once for every item of a list an earlier step made, a few at a "
                   "time, and collect what each one produced in order.",
        "canvas_required": True,
    },
]


def blocks() -> list[dict[str, Any]]:
    """Control blocks for the builder palette, placeable ones first."""
    return sorted(CONTROL_BLOCKS, key=lambda b: (b["canvas_required"], b["label"]))
