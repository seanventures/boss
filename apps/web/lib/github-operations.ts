import {
  BossApiError,
  type GitHubBranchLeaseSummary,
  type GitHubBranchPolicySummary,
  type GitHubBranchSnapshotSummary,
  type GitHubCodeSyncRunSummary,
  type GitHubConflictSetSummary,
  type GitHubConnectionSummary,
  type GitHubMergeAuthorizationSummary,
  type GitHubReconciliationRunSummary,
  type GitHubReconciliationSignalSummary,
  type GitHubRepositoryBindingSummary,
  type GitHubRepositoryRefStateSummary,
  type GitHubResolutionPlanSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type GitHubOperationsSummary = {
  activeConnections: number;
  activeRepositories: number;
  protectedRefs: number;
  activeLeases: number;
  divergentSnapshots: number;
  openConflicts: number;
  pendingMergeAuthorizations: number;
  reconciliationAttention: number;
  lastObservedAt?: string;
};

export type GitHubOperationsFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  warnings: string[];
  connections: GitHubConnectionSummary[];
  bindings: GitHubRepositoryBindingSummary[];
  policies: GitHubBranchPolicySummary[];
  snapshots: GitHubBranchSnapshotSummary[];
  leases: GitHubBranchLeaseSummary[];
  syncRuns: GitHubCodeSyncRunSummary[];
  conflicts: GitHubConflictSetSummary[];
  resolutionPlans: GitHubResolutionPlanSummary[];
  mergeAuthorizations: GitHubMergeAuthorizationSummary[];
  reconciliationRuns: GitHubReconciliationRunSummary[];
  signals: GitHubReconciliationSignalSummary[];
  refStates: GitHubRepositoryRefStateSummary[];
  summary: GitHubOperationsSummary;
};

type SettledList<T> = PromiseSettledResult<{ items: T[] }>;

function describeFailure(reason: unknown): string {
  if (reason instanceof BossApiError) {
    return `API returned ${reason.status}`;
  }
  return reason instanceof Error ? reason.message : "Unknown API error";
}

function itemsOrWarning<T>(
  result: SettledList<T>,
  label: string,
  warnings: string[],
): T[] {
  if (result.status === "fulfilled") {
    return result.value.items;
  }
  warnings.push(`${label}: ${describeFailure(result.reason)}`);
  return [];
}

function latestTimestamp(values: Array<string | undefined | null>): string | undefined {
  return values
    .filter((value): value is string => Boolean(value))
    .sort((left, right) => Date.parse(right) - Date.parse(left))[0];
}

function summarizeGitHubOperations(
  feed: Omit<GitHubOperationsFeed, "apiReachable" | "blockedReason" | "warnings" | "summary">,
): GitHubOperationsSummary {
  return {
    activeConnections: feed.connections.filter(
      (connection) => connection.status === "active",
    ).length,
    activeRepositories: feed.bindings.filter(
      (binding) => binding.status === "active" && !binding.archived,
    ).length,
    protectedRefs: feed.refStates.filter((ref) => ref.protected).length,
    activeLeases: feed.leases.filter((lease) => lease.status === "active").length,
    divergentSnapshots: feed.snapshots.filter((snapshot) =>
      ["diverged", "protected_blocked"].includes(snapshot.classification),
    ).length,
    openConflicts: feed.conflicts.filter((conflict) =>
      ["open", "resolution_proposed", "approved"].includes(conflict.status),
    ).length,
    pendingMergeAuthorizations: feed.mergeAuthorizations.filter((authorization) =>
      ["pending_approval", "authorized", "queued", "applying"].includes(
        authorization.status,
      ),
    ).length,
    reconciliationAttention: feed.reconciliationRuns.filter((run) =>
      ["failed", "deadlettered"].includes(run.status),
    ).length,
    lastObservedAt: latestTimestamp([
      ...feed.refStates.map((ref) => ref.observed_at),
      ...feed.snapshots.map((snapshot) => snapshot.observed_at),
      ...feed.reconciliationRuns.map((run) => run.updated_at),
    ]),
  };
}

export function emptyGitHubOperationsFeed(
  reason?: string,
  projectId = PROJECT_ID,
): GitHubOperationsFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    warnings: [],
    connections: [],
    bindings: [],
    policies: [],
    snapshots: [],
    leases: [],
    syncRuns: [],
    conflicts: [],
    resolutionPlans: [],
    mergeAuthorizations: [],
    reconciliationRuns: [],
    signals: [],
    refStates: [],
    summary: {
      activeConnections: 0,
      activeRepositories: 0,
      protectedRefs: 0,
      activeLeases: 0,
      divergentSnapshots: 0,
      openConflicts: 0,
      pendingMergeAuthorizations: 0,
      reconciliationAttention: 0,
    },
  };
}

export async function fetchGitHubOperationsFeed(
  projectId = PROJECT_ID,
  active = true,
): Promise<GitHubOperationsFeed> {
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyGitHubOperationsFeed("not requested", resolveProjectId(projectId));
  const resolvedProjectId = resolveProjectId(projectId);
  const results = await Promise.allSettled([
    client.listGitHubConnections(resolvedProjectId),
    client.listGitHubRepositoryBindings(resolvedProjectId),
    client.listGitHubBranchPolicies(resolvedProjectId),
    client.listGitHubBranchSnapshots(resolvedProjectId, { limit: 50 }),
    client.listGitHubBranchLeases(resolvedProjectId, {
      include_history: true,
      limit: 100,
    }),
    client.listGitHubCodeSyncRuns(resolvedProjectId, { limit: 50 }),
    client.listGitHubConflictSets(resolvedProjectId, { limit: 50 }),
    client.listGitHubResolutionPlans(resolvedProjectId, { limit: 50 }),
    client.listGitHubMergeAuthorizations(resolvedProjectId, { limit: 50 }),
    client.listGitHubReconciliationRuns(resolvedProjectId, { limit: 50 }),
    client.listGitHubReconciliationSignals(resolvedProjectId, { limit: 100 }),
    client.listGitHubRepositoryRefStates(resolvedProjectId, { limit: 200 }),
  ]);
  const warnings: string[] = [];
  const feedWithoutStatus = {
    projectId: resolvedProjectId,
    connections: itemsOrWarning<GitHubConnectionSummary>(
      results[0] as SettledList<GitHubConnectionSummary>,
      "Connections",
      warnings,
    ),
    bindings: itemsOrWarning<GitHubRepositoryBindingSummary>(
      results[1] as SettledList<GitHubRepositoryBindingSummary>,
      "Repositories",
      warnings,
    ),
    policies: itemsOrWarning<GitHubBranchPolicySummary>(
      results[2] as SettledList<GitHubBranchPolicySummary>,
      "Branch policies",
      warnings,
    ),
    snapshots: itemsOrWarning<GitHubBranchSnapshotSummary>(
      results[3] as SettledList<GitHubBranchSnapshotSummary>,
      "Branch snapshots",
      warnings,
    ),
    leases: itemsOrWarning<GitHubBranchLeaseSummary>(
      results[4] as SettledList<GitHubBranchLeaseSummary>,
      "Branch leases",
      warnings,
    ),
    syncRuns: itemsOrWarning<GitHubCodeSyncRunSummary>(
      results[5] as SettledList<GitHubCodeSyncRunSummary>,
      "Code sync",
      warnings,
    ),
    conflicts: itemsOrWarning<GitHubConflictSetSummary>(
      results[6] as SettledList<GitHubConflictSetSummary>,
      "Conflicts",
      warnings,
    ),
    resolutionPlans: itemsOrWarning<GitHubResolutionPlanSummary>(
      results[7] as SettledList<GitHubResolutionPlanSummary>,
      "Resolution plans",
      warnings,
    ),
    mergeAuthorizations: itemsOrWarning<GitHubMergeAuthorizationSummary>(
      results[8] as SettledList<GitHubMergeAuthorizationSummary>,
      "Merge authorizations",
      warnings,
    ),
    reconciliationRuns: itemsOrWarning<GitHubReconciliationRunSummary>(
      results[9] as SettledList<GitHubReconciliationRunSummary>,
      "Reconciliation",
      warnings,
    ),
    signals: itemsOrWarning<GitHubReconciliationSignalSummary>(
      results[10] as SettledList<GitHubReconciliationSignalSummary>,
      "Signals",
      warnings,
    ),
    refStates: itemsOrWarning<GitHubRepositoryRefStateSummary>(
      results[11] as SettledList<GitHubRepositoryRefStateSummary>,
      "Repository refs",
      warnings,
    ),
  };
  const successfulStreams = results.filter(
    (result) => result.status === "fulfilled",
  ).length;

  if (successfulStreams === 0) {
    return {
      ...emptyGitHubOperationsFeed(warnings.join("; "), resolvedProjectId),
      warnings,
    };
  }

  return {
    ...feedWithoutStatus,
    apiReachable: true,
    blockedReason: warnings.length > 0 ? "Some GitHub evidence is unavailable." : undefined,
    warnings,
    summary: summarizeGitHubOperations(feedWithoutStatus),
  };
}
