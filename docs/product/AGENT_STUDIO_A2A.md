# Agent Studio, Panels, and Global A2A Coordination Fabric

Status: Governing feature contract
Plane: BOSS-256 through BOSS-266
Date: July 26, 2026

## Product intent

Agent Studio is the governed authoring and operations environment for BOSS
agents. It lets an operator define what an agent is, which model policy it may
use, which rules and prompt layers constrain it, which capabilities it may
request, which tenant and data scope it may access, how it is tested, and which
immutable version is safe to run.

The global agent-to-agent (A2A) coordination fabric is the standard
communications substrate for agent instructions, status, task coordination,
joint execution, handoffs, resource requests, telemetry, replies,
acknowledgements, cancellation, replay, and failure visibility. Redis Streams
is the low-latency delivery transport. Postgres remains the canonical source of
truth for every envelope, delivery, acknowledgement, retry, deadletter, and
audit transition.

These capabilities extend the existing Role, Agent Module, Agent Group,
Workflow, Skill, Integration, Job, Budget, Receipt, and Assurance runtimes.
They do not replace or collapse them.

## Canonical boundaries

| Object | Owns | References but does not own |
| --- | --- | --- |
| `AgentDefinition` | Stable agent identity, purpose, lifecycle, owner, scope | Role, module, active version |
| `AgentVersion` | Immutable model policy, prompt layers, rules, requested capabilities, limits, provenance | RoleSpec, AgentModule, SkillSpec, IntegrationConnection |
| `AgentTestRun` | Scenario, compiled context, capability preview, assertions, evidence, result | AgentVersion, EvaluationRun, artifacts |
| `PanelDefinition` | Stable reusable panel identity and purpose | Active PanelVersion |
| `PanelVersion` | Version-pinned agents, independence, context, quorum, rounds, consolidation, dissent and escalation rules | AgentVersions, AgentGroupVersion |
| `PanelRun` | Pinned execution state, member runs, evidence, cost, disposition | WorkflowRun, AgentGroupRun, task/job/receipt refs |
| `A2AEnvelope` | Typed message identity and immutable addressing/lineage | Agent, panel, workflow, task, job, resource refs |
| `A2AChannel` | Scoped topic, schema and retention policy | Subscriptions |
| `A2ASubscription` | Consumer group identity, filters and delivery policy | AgentVersion or system consumer |
| `A2ADelivery` | Per-recipient attempt, transport cursor, lease/visibility and terminal state | Envelope, subscription |
| `A2AAcknowledgement` | Durable accepted/rejected/processed/deferred result and evidence | Delivery |

An agent is not a role. A role defines company responsibility and authority; an
agent is a versioned executable configuration that may implement a role. An
Agent Module remains the packaged install unit. A Panel is an explicit
composition profile over the Agent Group runtime. A Workflow remains the
versioned process map. A Skill is still the typed capability boundary, and an
IntegrationConnection is still a configured implementation of that capability.

## Agent definition contract

Every published `AgentVersion` must include:

- immutable version and content hash;
- RoleSpec and optional AgentModule lineage;
- model selection mode, preferred capabilities/model families, fallback policy,
  maximum calls/tokens and data-boundary rules;
- separate system, developer, job, and operator prompt layers;
- deterministic rules, required output contract, and forbidden behavior;
- requested SkillSpec and IntegrationDefinition identifiers;
- effective project, resource, artifact, task, and data scopes;
- autonomy/risk ceiling, approval posture, timeout and retry bounds;
- budget category, estimate and reservation requirements;
- context/memory policy and A2A publish/subscribe permissions;
- provenance, author, validation result, publish evidence and rollback target.

Publishing validates the requested configuration; it does not grant ambient
authority. At run time, BOSS resolves the intersection of the pinned
AgentVersion, RoleSpec, Agent Module, WorkflowStep or PanelVersion, current
project policy, installed Skills and IntegrationConnections, data boundary,
budget, health, and actor permission. A capability that is not in the
intersection is unavailable even if it appears in a prompt.

## Seeded agent library

The initial Studio catalog contains useful, editable definitions for:

- UX Engineer;
- UI Designer;
- Frontend Engineer;
- Backend Engineer;
- Full-Stack Engineer;
- Database Administrator;
- Chief Architect;
- Security Engineer;
- QA Engineer;
- Chief Operator / Job Captain;
- CFO / FinOps;
- DevOps Engineer.

Seeded definitions are versioned product assets, not privileged bypasses. Their
default execution posture is deterministic and side-effect free. Live model or
tool execution requires the same entitlement, BudgetReservation, lease,
approval, Receipt, and data-boundary gates as every other BOSS action.

## Panel contract

A `PanelVersion` pins every member `AgentVersion` and declares:

- purpose and expected output;
- named member slots and optional weights;
- parallel, producer-reviewer, debate, handoff, or custom bounded topology;
- shared versus private context;
- independence requirements and conflict-of-interest rules;
- quorum and maximum rounds;
- per-member and total call, time, and budget bounds;
- evidence requirements;
- consolidation policy;
- dissent preservation and minority-report rules;
- tie-break and escalation target;
- completion and cancellation rules.

Supported consolidation policies include attributed synthesis, evidence-ranked
selection, weighted recommendation, majority with mandatory minority report,
producer-reviewer revision, and hold-for-evidence. Consolidation never erases a
member output. A final answer stores the attributed findings, disagreements,
confidence and evidence used to form it. Quorum can finish the protocol; it
cannot make an unsupported claim true or bypass policy, budget, ownership,
approval, safety, or Assurance gates.

## A2A envelope

Every `A2AEnvelope` carries:

- globally unique message ID and project-scoped dedupe key;
- schema name and version;
- message kind;
- sender kind/ID/version and recipient selector;
- org/workspace/project and data-boundary scope;
- correlation, causation, conversation and trace IDs;
- optional task, job, workflow run/step, Agent Group run, Panel run, resource,
  ChangeSet, Decision, Receipt and artifact references;
- priority, TTL, not-before time and reply-to;
- typed JSON payload and payload hash;
- required acknowledgement mode;
- immutable created time and actor.

Initial message kinds:

- `instruction`;
- `status`;
- `task.coordinate`;
- `job.request`;
- `job.status`;
- `resource.request`;
- `resource.response`;
- `handoff`;
- `barrier.arrive`;
- `barrier.release`;
- `telemetry`;
- `finding`;
- `dissent`;
- `reply`;
- `cancel`;
- `heartbeat`;
- `error`.

Payload schemas are allowlisted and versioned. Secret material is forbidden;
messages carry SecretRefs only. Restricted or local-only data cannot publish to
a transport or subscriber that lacks the required boundary.

## Delivery architecture

The write path is:

`API validation -> Postgres envelope + deliveries + outbox -> leased dispatch job -> Receipt-backed Redis XADD -> stream cursor persisted -> consumer-group read -> durable delivery attempt -> actor-bound acknowledgement -> Receipt-backed Redis XACK -> terminal audit event`

The recovery path is:

`visibility timeout -> claim stale delivery -> bounded retry -> deadletter -> Decision/operator repair -> replay with new message ID and causation link`

Redis Streams provides ordered per-channel delivery, consumer groups, pending
entry recovery, and lag metrics. It never owns canonical business state.
Losing Redis may delay coordination but cannot erase instructions or audit
history. The Postgres outbox is replayable after recovery.

No component silently falls back to in-memory delivery in production. When
Redis is absent or unhealthy, transport health is `degraded`, queued envelopes
remain durable, attempts retry or deadletter visibly, and operators receive a
repair path.

## Ops and coordination API

The API provides:

- agent definitions, immutable versions, validation, publish/retire and run
  creation;
- panel definitions, immutable versions, runs, member results, findings and
  dissent;
- A2A channels, subscriptions, publish, poll, acknowledge, replay and cancel;
- query envelopes by state and trace, with conversation, correlation,
  causation, sender, recipient and runtime lineage in each result;
- transport health, pending/in-flight deliveries, attempts, retries and
  deadletters;
- typed job, resource, handoff, barrier and status messages;
- operator repair and replay controls.

Mutating API operations require the existing project RBAC. Creating executable
jobs or resource changes still routes through the Job, Workflow and Resource
Plane services; an A2A message by itself is never authority to spend or actuate.

## Studio experience

The Studio is a dense technical workbench:

- left library: agents, panels and workflows with version, readiness and scope;
- center canvas: selected topology and live run/message paths;
- right inspector: prompt/rules/model/tools/scope, gates, evidence and version
  lineage;
- lower trace: live A2A messages, delivery state, acknowledgements,
  failures and repair;
- test console: scenario input, compiled capability preview and evidence;
- publish controls: validation, diff, gate result, canary posture and rollback.

The UI must distinguish configured, effective, denied and unavailable
capabilities. It must render partial API failure without discarding healthy
evidence. It supports keyboard navigation, responsive layouts, reduced motion,
loading/empty/error/blocked/degraded/healthy states, and exact canonical IDs.

## Security and governance

- Never place raw provider or model credentials in prompts, versions or A2A
  payloads.
- Validate tenant scope again at publish, dispatch, poll and acknowledgement.
- Reject unknown message schemas and oversize payloads.
- Bound TTL, retries, stream retention, calls, tokens, runtime and panel rounds.
- Require a lease for every executable job.
- Require a BudgetReservation and Receipt for every spendy model/tool call.
- Require a ChangeSet and ResourceReceipt for provider writes.
- Preserve immutable audit lineage for edit, publish, run, message, delivery,
  acknowledgement, replay, cancellation and deadletter repair.
- Do not let a panel, workflow or bus message widen RoleSpec or project
  authority.

## Acceptance proof

The feature is release-ready when tests prove:

- immutable versioning and content-dedupe;
- seeded catalog idempotency;
- capability intersection and revoked-connection denial;
- tenant isolation and actor permissions;
- deterministic zero-effect agent execution;
- panel partial failure, disagreement, quorum without evidence, timeout and
  preserved dissent;
- transactional outbox creation and duplicate publish prevention;
- Redis publish, consumer-group delivery, acknowledgement and pending-state
  reporting;
- Redis outage, reclaim, bounded retry, deadletter and replay;
- correlation/causation and task/job/workflow/panel audit queries;
- no raw secrets and correct data-boundary denial;
- no spendy execution without reservation;
- visual loading, empty, degraded, partial, blocked, active and completed states.
