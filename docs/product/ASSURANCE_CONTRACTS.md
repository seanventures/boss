# Ground Truth, Assurance, Agent Group, and Integration Contracts

Status: SpecPack object and reusable-workflow contract layers implemented for
Plane BOSS-175–214.

## Purpose

BOSS cannot depend on a model remembering settled facts, resisting its priors,
or verifying its own claims. The durable contracts in this package move those
responsibilities into versioned records that deterministic policy and runtime
services can enforce.

This delivery intentionally implements the contract layer before the API and
persistence layer. Paramg currently owns the OSS engine relocation, so these
objects establish the stable seam that the post-split runtime can consume
without creating a second service implementation or conflicting schema.

## Contract catalog

| Plane | Contract | SpecPack object | Governing feature |
| --- | --- | --- | --- |
| BOSS-175 | GroundTruthMetricDefinition | `ground_truth_metric_definition.yaml` | GT-1 metrics |
| BOSS-176 | GroundTruthObservation | `ground_truth_observation.yaml` | GT-1 metrics |
| BOSS-177 | RegressionSignal | `regression_signal.yaml` | GT-1 metrics |
| BOSS-178 | SettledConclusion | `settled_conclusion.yaml` | GT-2 closed questions |
| BOSS-179 | ClosedQuestionMatch | `closed_question_match.yaml` | GT-2 closed questions |
| BOSS-180 | Claim | `claim.yaml` | GT-3 evidence gate |
| BOSS-181 | Evidence | `evidence.yaml` | GT-3 evidence gate |
| BOSS-182 | ClaimVerification | `claim_verification.yaml` | GT-3 evidence gate |
| BOSS-183 | SurfaceOwnership | `surface_ownership.yaml` | GT-4 single deployer |
| BOSS-184 | OwnershipTransition | `ownership_transition.yaml` | GT-4 single deployer |
| BOSS-185 | VerificationPolicy | `verification_policy.yaml` | GT-5 standing verifier |
| BOSS-186 | VerificationRun | `verification_run.yaml` | GT-5 standing verifier |
| BOSS-187 | AssuranceCase | `assurance_case.yaml` | GT-6 assurance graph |
| BOSS-188 | AssurancePolicySnapshot | `assurance_policy_snapshot.yaml` | GT-6 policy composition |
| BOSS-189 | AgentGroupSpec | `agent_group_spec.yaml` | Agent Groups |
| BOSS-190 | AgentGroupRun | `agent_group_run.yaml` | Agent Groups |
| BOSS-191 | IntegrationDefinition | `integration_definition.yaml` | Integrations |
| BOSS-192 | IntegrationConnection | `integration_connection.yaml` | Integrations |
| BOSS-193 | ExecutorBinding | `executor_binding.yaml` | Integrations |
| BOSS-194 | CapabilityResolution | `capability_resolution.yaml` | Integrations |

All twenty objects are registered in `packages/specpack/manifest.yaml` under
the `ground_truth`, `agent_groups`, and `integrations` catalogs.

## Workflow catalog

The matching workflows are native `WorkflowSpec` graphs. Bootstrap registers
and publishes them idempotently in the existing reusable Workflow Runtime.
Registration does not create a work-class mapping: an administrator must still
choose the workflow explicitly or create a scoped mapping before it can run.

| Plane | Workflow | Contract |
| --- | --- | --- |
| BOSS-195 | Metric evaluation | `ground_truth_metric_evaluation.yaml` |
| BOSS-196 | Baseline change approval | `ground_truth_baseline_change.yaml` |
| BOSS-197 | Regression signal lifecycle | `regression_signal_lifecycle.yaml` |
| BOSS-198 | Closed-question publication guard | `closed_question_publication_guard.yaml` |
| BOSS-199 | Settled-conclusion reopen | `settled_conclusion_reopen.yaml` |
| BOSS-200 | Claim authority gate | `claim_authority_gate.yaml` |
| BOSS-201 | Red-to-green verification | `red_to_green_verification.yaml` |
| BOSS-202 | Causal isolation verification | `causal_isolation_verification.yaml` |
| BOSS-203 | Surface ownership acquisition | `surface_ownership_acquire.yaml` |
| BOSS-204 | Surface ownership transfer | `surface_ownership_transfer.yaml` |
| BOSS-205 | Ownership effect preflight | `ownership_effect_preflight.yaml` |
| BOSS-206 | Verification scheduling | `verification_schedule.yaml` |
| BOSS-207 | Adversarial verification execution | `adversarial_verification_execute.yaml` |
| BOSS-208 | Assurance Case evaluation | `assurance_case_evaluate.yaml` |
| BOSS-209 | Panel Agent Group protocol | `agent_group_panel.yaml` |
| BOSS-210 | Producer-reviewer Agent Group protocol | `agent_group_producer_reviewer.yaml` |
| BOSS-211 | Bounded-debate Agent Group protocol | `agent_group_bounded_debate.yaml` |
| BOSS-212 | Fan-out/join Agent Group protocol | `agent_group_fan_out_join.yaml` |
| BOSS-213 | Integration connection lifecycle | `integration_connection_lifecycle.yaml` |
| BOSS-214 | Capability resolution gate | `capability_resolution_gate.yaml` |

The manifest catalogs all twenty under `workflows.ground_truth`,
`workflows.agent_groups`, and `workflows.integrations`. Contract tests require
typed input/output schemas, a dedupe key, runtime-valid acyclic control flow,
fail-closed decisions, evidence artifacts, and no implicit selector or mapping.

## Enforced invariants

### Ground truth and claims

- Metric baselines cannot silently normalize a degraded system. Baseline
  changes require permission, reason, and an audit event.
- Evaluations carry an exact metric version, lease, heartbeat, evidence, and
  dedupe key. Evaluator failure is visible and cannot report healthy.
- A direct regressed-prior recommendation is blocked before publication and
  returns the settled conclusion, cause menu, and evidence. Quotation,
  historical discussion, negation, and hypotheticals are classified
  separately.
- Fixed and solved claims require red-to-green evidence. Caused-by requires an
  isolation experiment. Regressed requires a comparable passing baseline.
  Missing or conflicting proof is inconclusive, never success.

### Ownership and verification

- A production surface/environment has one current deploying owner.
  Transitions use an expected ownership version and reject stale writers.
- Provider boundaries must revalidate ownership immediately before an effect.
  An unavailable ownership registry fails closed.
- Standing verification runs are leased, deduplicated, retry/deadletter aware,
  and end only as passed, refuted, or inconclusive.
- Model or tool spend requires normal BOSS budget reservations and receipts.
  Refuted results reopen or block authoritative claims.

### Assurance composition

- Assurance Cases reference versioned observations, claims, evidence,
  conclusions, ownership records, verifier runs, decisions, workflows, groups,
  skills, and integrations. They do not copy unversioned prose from those
  systems.
- Policy snapshots are immutable and content-addressed. Lower scopes cannot
  weaken mandatory evidence, safety, ownership, verifier, or budget rules.
  Conflicts fail closed into an actionable Decision.

### Agent Groups and Integrations

- Agent Groups define collaboration topology and completion protocol, while the
  Workflow Runtime owns durable sequencing. Consensus and quorum cannot bypass
  an evidence, approval, ownership, safety, or budget gate.
- Integration definitions describe supported skills and trusted executor
  types; installed connections hold only SecretRefs and scoped grants.
- Capability resolution is an intersection of role/group, workflow step,
  tenant scope, connection health, policy, risk, and data boundary. No
  participant can expand the installed connection's authority.
- External writes require Receipts and dedupe keys. Provider writes also
  require ChangeSets and ResourceReceipts. Spendy effects require budget
  reservations.

## Runtime boundary

These contracts do not claim that the generalized Ground Truth or Agent Group
action executors are complete. Durable orchestration is now represented by
published native workflows, while the next vertical layer after the engine
split lands is:

1. canonical migrations and repositories for the contract records;
2. deterministic action executors, policy evaluators, and state-transition
   services for the workflow operation names;
3. leased metric/verifier workers with retries and deadletters;
4. API/client endpoints and Mission Control views; and
5. end-to-end tests proving tenant isolation, concurrency safety, fail-closed
   behavior, and evidence-backed terminal state.

The object and workflow contract tests live in
`apps/api/tests/test_assurance_specpack_contracts.py` and
`apps/api/tests/test_assurance_workflow_contracts.py`. They prevent catalog
drift, invalid runtime graphs, missing authority fields, permissive result
states, raw credential fields, implicit workflow mappings, and governance
bypass in Agent Group protocols.
