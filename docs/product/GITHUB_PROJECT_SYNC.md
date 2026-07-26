# GitHub Project Sync, Branch Orchestration, and Conflict Resolution

Status: Governing feature contract

Plane: BOSS-246 parent; BOSS-247 through BOSS-255 implementation slices

## Outcome

BOSS connects GitHub per project and turns repository/branch state into
durable, policy-governed operational state. Multiple contributors and agents
can work concurrently without overwriting one another, confusing a rewritten
history with a content conflict, or merging against stale remote truth.

## Product boundary

This feature composes existing GitHub App authentication, community webhooks,
software-factory PRs, pipeline verdicts, receipts, ChangeSets, resource
receipts, Decisions, jobs, leases, events, and evidence bundles. It does not
create a second Git client outside the adapter boundary and does not give
agents raw credentials.

Global `BOSS_TARGET_GITHUB_OWNER`, `BOSS_TARGET_GITHUB_REPO`, App settings, and
PAT settings remain a compatibility bootstrap. The target architecture resolves
every operation from a `ProjectGitHubConnection` and
`GitHubRepositoryBinding`.

## Canonical objects

| Object | Responsibility |
| --- | --- |
| `ProjectGitHubConnection` | Project-scoped authentication, SecretRef posture, App installation identity, effective permissions, health, disable/revoke lifecycle |
| `GitHubRepositoryBinding` | Canonical project repository target and primary/source/upstream/mirror role |
| `BranchPolicy` | Protected refs, naming, update/merge methods, evidence/check/approval rules |
| `BranchLease` | Fenced TTL ownership of a mutable ref/worktree by one run |
| `BranchSnapshot` | Immutable fetched remote topology, commits, trees, merge bases, ahead/behind and protection/check state |
| `CodeSyncRun` | Replay-safe synchronization classification, plan, effect evidence, result and rollback |
| `ConflictSet` | Typed base/ours/theirs conflicts with file and hunk provenance |
| `ResolutionPlan` | Proposed resolutions, validations, reviewer result, dissent and Decision |
| `MergeAuthorization` | Expiring authorization over exact base/head snapshots and required proof |
| `ReconciliationSignal` | Deduplicated webhook delivery/ref/SHA claim and its applied, stale, retry, or deadletter state |
| `GitHubReconciliationRun` | Leased webhook, poll, or repair execution with attempt, rate-limit, Receipt, observation, and deadletter lineage |
| `RepositoryObservation` | Immutable Receipt-backed provider observation for one ref during one run |
| `RepositoryRefState` | Latest reconciled branch existence, commit/tree, protection, signal, observation, and Receipt truth |

## Invariants

1. Credentials are named SecretRefs; raw values never persist or enter prompts.
2. Every operation resolves a project/repository binding and current lifecycle.
3. Every remote read or write has a dedupe key and Receipt. Every write also
   has a ChangeSet and ResourceReceipt.
4. Every mutable branch/worktree has one fenced lease with TTL and heartbeat.
5. Every decision starts from a fresh remote snapshot.
6. Equal trees are content-equivalent even when commit identities were
   rewritten; BOSS records lineage instead of manufacturing a conflict.
7. Fast-forward is preferred. Divergence creates a resolution branch.
8. BOSS never force-updates a protected ref or another contributor's branch.
9. Conflict resolution requires reproducible validation and independent review.
10. Merge authorization binds exact snapshots, checks, evidence, approvals,
    ownership, policy, expiry, and rollback.
11. Webhook and polling paths converge through the same jobs/dedupe/state
    machine; failures retry, deadletter, or escalate visibly.

## Topology classification

| Classification | Meaning | Default action |
| --- | --- | --- |
| `equal` | Same commit and tree | No-op |
| `tree_equivalent_rewrite` | Different commit IDs, equivalent tree/patch | Preserve remote identity, record lineage |
| `fast_forward` | Target can move without losing commits | Plan fast-forward |
| `behind` | Local/owned branch needs remote commits | Synchronize owned branch |
| `diverged` | Both sides contain unique content | Create resolution branch and ConflictSet |
| `missing` | Expected ref is absent | Create only if policy and ownership allow |
| `protected_blocked` | Policy/protection forbids action | Safe-HOLD Decision |

## Workflow

```text
Resolve project binding
  -> fetch BranchSnapshot
  -> evaluate BranchPolicy
  -> classify topology
  -> no-op / fast-forward / resolution branch / Decision
  -> plan ChangeSet and rollback
  -> acquire fenced BranchLease for the exact mutable ref
  -> refetch and reject stale topology/policy
  -> execute adapter effect
  -> write Receipt + ResourceReceipt for every provider write attempt
  -> refetch and verify result snapshot
  -> attach evidence and rollback point
  -> release lease
```

## Delivery slices

- BOSS-247: project connection, repository binding, SecretRef boundary,
  permission discovery, lifecycle, audit, and receipted test.
- BOSS-248: branch policy, protected refs, naming, allowed merge/update methods.
- BOSS-249: remote snapshots, commit/tree topology and divergence
  classification.
- BOSS-250: branch/worktree leases, fencing, heartbeat, reclaim, ownership.
- BOSS-251: safe synchronization and rewritten-history semantics.
- BOSS-252: ConflictSets, resolution branches, red-to-green evidence.
- BOSS-253: merge queue, MergeAuthorization, approvals and rollback.
- BOSS-254: webhook/poll reconciliation, retry and deadletters.
- BOSS-255: Mission Control repository topology and branch operations UX.

## BOSS-247 shipped contract

The foundation provides project-scoped GitHub App and PAT connections,
project-scoped primary/source/upstream/mirror repository bindings,
`github_manage` authorization, SecretRef validation, secret-shaped metadata
rejection, effective repository permission discovery, deduplicated
`github.repository.read` Receipts, audit events, disable/enable, and irreversible
revoke. Responses expose only `credential_configured`, never the credential
pointer.

## BOSS-248 and BOSS-249 implemented contract

Branch policies are immutable versions. Revising a policy supersedes the prior
record and preserves protected-ref patterns, allowed branch prefixes, bounded
name rules, update and merge methods, required checks/approvals, pull-request,
linear-history and signed-commit requirements, automation posture, force-push
posture, actor, and metadata. Evaluation is deterministic and returns
`allowed`, `approval_required`, or `blocked` with machine-readable reasons.

Branch snapshots are immutable and tenant-scoped. A capture resolves the
project binding and SecretRef-backed adapter, reads both remote heads and tree
identities, parents, merge base, ahead/behind counts, branch protection, and
target check runs, pins the active policy version, and records a
`github.repository.snapshot` Receipt. Replaying the dedupe key returns the
existing snapshot without another provider call. Provider failures create
redacted failed Receipt and event evidence.

The classifier retains both raw topology and the policy result. It recognizes
equal commits, tree-equivalent identity rewrites, fast-forward, behind,
diverged, and missing refs. A topologically valid fast-forward that policy
denies is preserved as `topology_classification=fast_forward` with final
`classification=protected_blocked`.

## BOSS-250 implemented contract

Every mutable repository ref can have exactly one active `BranchLease`.
Acquisition serializes on the repository binding, expires stale ownership
inside the same transaction, and assigns a monotonically increasing fencing
token. The raw high-entropy lease token is returned only on first acquisition;
only its SHA-256 hash persists and exact acquire-key replay does not return the
raw token again.

Heartbeat and release require both the raw lease token and current fencing
token. Expired leases cannot heartbeat or release, unexpired leases cannot be
administratively reclaimed, and reacquisition after expiry retains
`reclaimed_from_lease_id` lineage with a higher fence. Lease state carries
owner kind/ID, run, worktree, ref, TTL, actor, heartbeat, release/reclaim reason,
and audit events without putting the raw token in events or read models.

## BOSS-251 implemented contract

`CodeSyncRun` planning consumes one immutable `BranchSnapshot` and pins its
repository binding, exact policy ID/version, source/target commit and tree
identities, topology, dedupe key, rollback ref/SHA, and canonical `ChangeSet`.
Equal topology is a no-op and a tree-equivalent rewrite preserves remote commit
identity. An owned branch that is behind can be advanced without force.
Protected-target updates remain `pending_approval`; this slice cannot bypass
the later `MergeAuthorization` gate.

Apply requires the raw lease token and current monotonic fence for the exact
mutable ref. Immediately before any provider write BOSS verifies that the
active policy is still the pinned version and captures a fresh remote snapshot.
Changed commits, trees, refs, or topology fail safely before mutation.

Every GitHub mutation is an adapter operation backed by one `ChangeSet`
operation, `Receipt`, and `ResourceReceipt`. Zero-cost GitHub ref operations
record that no `BudgetReservation` is required. Ref updates always use
`force=false`. Provider failures are redacted, retryable for a bounded three
attempts, and terminal failure creates a visible deadletter. Successful
fast-forwards capture a result snapshot and retain an explicit rollback point.

## BOSS-252 implemented contract

Divergence creates a policy-approved `ai/` resolution branch at the immutable
source head and attempts to merge the immutable target head there. The
protected target is never touched. A clean provider merge leaves the resolution
branch ready for the later merge-authorization slice. A provider conflict
creates a typed `ConflictSet` containing base/ours/theirs commit and tree
identities, the resolution-branch head, and bounded changed-file/patch
evidence.

A `ResolutionPlan` may modify only files represented by the conflict evidence,
accepts bounded UTF-8 text and executable/non-executable file modes, and
requires reproducible red-to-green evidence: the named red command must have a
failing exit code and the green command must pass, with evidence references for
both. Approval must come from a `github_manage` actor other than the plan
creator and must attach an approved review verdict and summary.

Apply re-verifies the exact resolution-branch and target identities, active
policy, lease token, and fence. It constructs an explicit two-parent commit
through the Git Data adapter, then advances only the resolution branch with a
non-force update. Both provider actions have independent ChangeSet operations,
Receipts, and ResourceReceipts. The protected target remains unchanged until
BOSS-253 supplies a current `MergeAuthorization`.

## BOSS-253 implemented contract

`MergeAuthorization` is a durable, expiring grant over one exact pull-request
snapshot. Requesting authorization observes and pins the project repository
binding, active branch-policy ID/version, PR number and URL, source/target refs,
head/base commit and tree identities, required checks, effective reviews,
evidence bundle, merge method, and optional CodeSyncRun/ResolutionPlan
lineage. The observation is Receipt-backed and creates a zero-cost,
risk-tier-three ChangeSet with a non-destructive revert-PR rollback plan.

Approval must come from a `github_manage` actor other than the requester and
does not extend expiry. Authorized entries enter a deterministic per-repository
target queue; an entry cannot run while an earlier queued/applying/retryable
entry owns that target.

Apply requires the raw lease token and monotonic fence for the exact protected
target. Immediately before the provider write BOSS re-observes the PR and
rejects changed policy, refs, commits, trees, target head, draft/closed or
unmergeable posture, missing/failing required checks, stale approvals, or a
current request for changes. The adapter performs a second exact-snapshot guard
and submits the merge through the GitHub pull-request API with the authorized
head SHA; it never directly pushes or force-updates the target ref.

Every observation and verification read has a deduplicated Receipt. Every
merge attempt has one canonical ChangeSet operation, Receipt, and
ResourceReceipt. Successful apply verifies the resulting target commit/tree
and records the previous target SHA plus a manual, newly authorized revert-PR
plan. Provider failures are redacted, retry at most three times, and become a
visible deadletter.

## BOSS-254 implemented contract

Verified GitHub webhook delivery is now only a low-latency signal. Inbound
routing first resolves the active project repository binding, persists the raw
delivery once, and creates one deduplicated `ReconciliationSignal` and leased
`github.repository.reconcile` job per affected branch. The webhook request
never calls GitHub. Scheduled polling uses the same run and worker path and
discovers the default branch plus refs referenced by active leases, sync runs,
merge authorizations, and prior observations.

The leased worker reads the GitHub core rate limit before branch state and
records every provider read as a normal `Receipt`. It then persists immutable
per-run observations and updates the canonical `RepositoryRefState` with exact
existence, commit SHA, tree SHA, protection, observation, signal, and Receipt
lineage. A webhook claim whose expected SHA is no longer current is explicitly
`stale`; a late delete followed by reappearance is
`unexpected_reappearance`. Neither can roll canonical truth backward.

Rate exhaustion and provider failures use the durable job retry path with
bounded exponential or provider-reset-aware backoff. The run and signal expose
each failed attempt, redacted error, next attempt, and terminal deadletter.
Operators can schedule an explicit repair run linked to the failed original.
Fresh reconciled state also blocks pending sync or merge work whose pinned
commit/tree identity drifted, preventing stale authorization from surviving a
missed or out-of-order webhook.

The typed API and client expose reconciliation runs, poll scheduling, leased
execution, repairs, webhook signals, and current repository ref state. BOSS
workers advertise the reconciliation capability and do not double-finalize
the job lease because the reconciler atomically owns its success/retry/
deadletter transition with its observation evidence.

## BOSS-255 implemented contract

Mission Control now has a dedicated GitHub workspace scoped to the active BOSS
project and actor. Its feed consumes the canonical connection, binding, policy,
snapshot, lease, sync, conflict, resolution, merge-authorization,
reconciliation, signal, and repository-ref APIs. Each stream fails
independently, so an unavailable evidence source is named without hiding
healthy repository truth.

The primary workspace keeps live branch topology and the selected branch's
commit/tree, protection, immutable observation, Receipt, ahead/behind
classification, active fenced writer, sync state, merge state, and rollback
identity together. A linear delivery view exposes code sync, red-to-green
conflict resolution, independent review, merge authorization, and
webhook/poll/repair convergence without inventing state or bypassing the
canonical records.

Actors holding `github_manage` can configure SecretRef-backed connections,
bind repositories, create immutable branch-policy versions, test access,
capture topology snapshots, plan code synchronization, approve independently
reviewed resolution plans, authorize and enqueue merges, request polling, and
repair terminal reconciliation runs. Read-only actors see the same evidence
with mutation controls withheld. Operations that require a one-time branch
lease secret and fencing token remain worker-only rather than exposing those
credentials in the browser.

The responsive surface includes explicit unconfigured, empty, partial-error,
and healthy states. Automated rendering and feed tests cover canonical summary
derivation, partial degradation, branch/Receipt evidence, workflow state, and
permission gating; TypeScript validation and the production Next.js build
exercise the integrated console route.

## Completion criteria

The parent feature is complete only when all nine slices are shipped, the
Mission Control topology is backed by canonical records, branch state-machine
transitions have acceptance tests, provider effects have ChangeSet/receipt
coverage, rewritten-history equivalence is regression tested, conflicts have
red-to-green proof, and protected refs cannot be mutated without current
authorization.
