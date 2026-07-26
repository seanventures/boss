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
  -> acquire fenced BranchLease
  -> fetch BranchSnapshot
  -> evaluate BranchPolicy
  -> classify topology
  -> no-op / fast-forward / resolution branch / Decision
  -> plan ChangeSet and authorization
  -> execute adapter effect
  -> write ResourceReceipt
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

## Completion criteria

The parent feature is complete only when all nine slices are shipped, the
Mission Control topology is backed by canonical records, branch state-machine
transitions have acceptance tests, provider effects have ChangeSet/receipt
coverage, rewritten-history equivalence is regression tested, conflicts have
red-to-green proof, and protected refs cannot be mutated without current
authorization.
