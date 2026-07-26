import {
  BossApiError,
  type ChangeSetSummary,
  type CostAnomalySummary,
  type CostEventSummary,
  type DeploymentEnvironmentPolicySummary,
  type DeploymentPromotionSummary,
  type DeploymentRunSummary,
  type DeploymentTargetSummary,
  type DriftReportSummary,
  type ExternalResourceSummary,
  type ProviderAccountSummary,
  type ProviderDefinitionSummary,
  type ProviderHealthSummary,
  type ReconciliationRunSummary,
  type ResourceReceiptSummary,
  type ScalePlanSummary,
  type WorkerPoolSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type ProviderAccountFeedItem = {
  id: string;
  provider: string;
  accountLabel: string;
  authMethod: string;
  credentialRef?: string;
  credentialRefs: Record<string, string>;
  connectionMetadata: Record<string, unknown>;
  scopes: string[];
  status: string;
  ownerActor?: string;
  credentialVersion: number;
  credentialRotatedAt?: string;
  disabledAt?: string;
  disabledBy?: string;
  revokedAt?: string;
  revokedBy?: string;
  readOnly: boolean;
  deployCapable: boolean;
  billingReadable: boolean;
  destructiveActionsDisabled: boolean;
  capabilities: string[];
  missingPermissions: string[];
  lastTestStatus?: string;
  lastTestedAt?: string;
  lastError?: Record<string, unknown>;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
};

export type ProviderCredentialSlotFeedItem = {
  key: string;
  secretKind: string;
  required: boolean;
  defaultRef?: string;
};

export type ProviderAuthMethodFeedItem = {
  key: string;
  runtimeStatus: string;
  credentialSlots: ProviderCredentialSlotFeedItem[];
};

export type ProviderDefinitionFeedItem = {
  provider: string;
  displayName: string;
  category: string;
  adapterStatus: string;
  defaultAuthMethod: string;
  authMethods: ProviderAuthMethodFeedItem[];
  capabilities: string[];
};

export type ExternalResourceFeedItem = {
  id: string;
  providerAccountId?: string;
  provider: string;
  resourceType: string;
  externalId: string;
  name: string;
  state: string;
  ownership: string;
  environment: string;
  ownerRole?: string;
  taskId?: string;
  deploymentTargetId?: string;
  receiptId?: string;
  budgetAllocationId?: string;
  healthStatus: string;
  estimatedMonthlyUsdMicros?: number;
  actualMonthlyUsdMicros?: number;
  costConfidence: string;
  lastSeenAt: string;
  tags: string[];
  metadata: Record<string, unknown>;
  updatedAt: string;
};

export type CostEventFeedItem = {
  id: string;
  providerAccountId?: string;
  externalResourceId?: string;
  receiptId?: string;
  provider: string;
  category: string;
  amountUsdMicros: number;
  costLayer: string;
  confidence: string;
  sourceRef?: string;
  createdAt: string;
};

export type CostAnomalyFeedItem = {
  id: string;
  providerAccountId?: string;
  costEventId?: string;
  decisionId?: string;
  provider: string;
  anomalyType: string;
  severity: string;
  status: string;
  amountUsdMicros: number;
  thresholdUsdMicros: number;
  summary: string;
  recommendedActions: Array<Record<string, unknown>>;
  createdAt: string;
};

export type ChangeSetFeedItem = {
  id: string;
  providerAccountId?: string;
  title: string;
  summary: string;
  status: string;
  riskTier: number;
  operations: Array<Record<string, unknown>>;
  expectedCostUsdMicros: number;
  reservationId?: string;
  approvalDecisionId?: string;
  rollbackPlan: string[];
  createdBy: string;
  failureReason?: string;
  createdAt: string;
  updatedAt: string;
};

export type ResourceReceiptFeedItem = {
  id: string;
  changesetId?: string;
  providerAccountId?: string;
  dedupeKey: string;
  provider: string;
  action: string;
  resourceType: string;
  resourceRef?: string;
  status: string;
  providerRef?: string;
  createdAt: string;
  updatedAt: string;
};

export type DeploymentTargetFeedItem = {
  id: string;
  appName: string;
  repo: string;
  environment: string;
  providerAccountId?: string;
  targetKind: string;
  targetRef: string;
  domains: string[];
  secretRefs: string[];
  status: string;
  createdAt: string;
};

export type DeploymentRunFeedItem = {
  id: string;
  deploymentTargetId: string;
  changesetId?: string;
  decisionId?: string;
  releaseArtifactId?: string;
  healthArtifactId?: string;
  rollbackArtifactId?: string;
  appName: string;
  environment: string;
  repo: string;
  targetKind: string;
  targetRef: string;
  commitRef?: string;
  status: string;
  rollbackPlan: string[];
  healthCheckResults: Array<Record<string, unknown>>;
  failureReason?: string;
  createdAt: string;
};

export type DeploymentEnvironmentPolicyFeedItem = {
  id: string;
  environment: string;
  runtimeMode: string;
  allowedTargetKinds: string[];
  requiresChangeSet: boolean;
  requiresBudgetReservation: boolean;
  requiresApproval: boolean;
  requiresHealthCheck: boolean;
  requiresRollbackPlan: boolean;
  rolloutNotes?: string;
  status: string;
  createdAt?: string;
};

export type DeploymentPromotionFeedItem = {
  id: string;
  appName: string;
  repo: string;
  commitRef: string;
  status: string;
  pipelineStatus: string;
  gateStatus: string;
  stagingRunStatus?: string;
  productionRunStatus?: string;
  rolloutStatus?: string;
  rolloutStage?: string;
  rollbackReason?: string;
  failureReason?: string;
  timeline: Array<Record<string, unknown>>;
  createdAt: string;
};

export type ProviderHealthFeedItem = {
  id: string;
  providerAccountId?: string;
  provider: string;
  status: string;
  apiAvailable: boolean;
  rateLimitStatus: string;
  usageCapStatus: string;
  lastCheckedAt: string;
};

export type DriftReportFeedItem = {
  id: string;
  providerAccountId?: string;
  classification: string;
  summary: string;
  unmanagedCount: number;
  orphanedCount: number;
  securityImpactingCount: number;
  productionImpactingCount: number;
  createdAt: string;
};

export type ReconciliationRunFeedItem = {
  id: string;
  providerAccountId?: string;
  provider?: string;
  status: string;
  resourcesSeen: number;
  resourcesChanged: number;
  costsSeen: number;
  startedBy: string;
  startedAt: string;
  finishedAt?: string;
  inventorySnapshotArtifactId?: string;
};

export type WorkerPoolFeedItem = {
  id: string;
  poolKey: string;
  kind: string;
  providerAccountId?: string;
  provider?: string;
  status: string;
  currentWorkers: number;
  desiredWorkers: number;
  minWorkers: number;
  maxWorkers: number;
  maxQueueDepthPerWorker: number;
  costPerWorkerHourUsdMicros: number;
  capabilities: string[];
  createdAt: string;
};

export type ScalePlanFeedItem = {
  id: string;
  workerPoolId?: string;
  status: string;
  triggerReason: string;
  queueDepth: number;
  desiredWorkersBefore: number;
  desiredWorkersAfter: number;
  targetWorkers: number;
  budgetState: string;
  budgetAvailableUsdMicros: number;
  estimatedHourlyDeltaUsdMicros: number;
  providerHealthStatus?: string;
  actions: Array<Record<string, unknown>>;
  recommendations: Array<Record<string, unknown>>;
  changesetId?: string;
  decisionId?: string;
  createdAt: string;
};

export type ProviderOpsSummary = {
  connectedProviders: number;
  healthyProviders: number;
  openChangeSets: number;
  resourceCount: number;
  unmanagedResourceCount: number;
  orphanedSignalCount: number;
  spendUsdMicros: number;
  openCostAnomalies: number;
  desiredWorkerCount: number;
  openScalePlans: number;
  activeDeploymentRuns: number;
};

export type ProviderOpsFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  partialFailures: string[];
  refreshedAt: string;
  providerDefinitions: ProviderDefinitionFeedItem[];
  providerAccounts: ProviderAccountFeedItem[];
  resources: ExternalResourceFeedItem[];
  costs: CostEventFeedItem[];
  costAnomalies: CostAnomalyFeedItem[];
  changeSets: ChangeSetFeedItem[];
  resourceReceipts: ResourceReceiptFeedItem[];
  deploymentTargets: DeploymentTargetFeedItem[];
  deploymentPolicies: DeploymentEnvironmentPolicyFeedItem[];
  deploymentRuns: DeploymentRunFeedItem[];
  deploymentPromotions: DeploymentPromotionFeedItem[];
  providerHealth: ProviderHealthFeedItem[];
  driftReports: DriftReportFeedItem[];
  reconciliationRuns: ReconciliationRunFeedItem[];
  workerPools: WorkerPoolFeedItem[];
  scalePlans: ScalePlanFeedItem[];
  summary: ProviderOpsSummary;
};

function mapProviderDefinition(
  payload: ProviderDefinitionSummary,
): ProviderDefinitionFeedItem {
  return {
    provider: payload.provider,
    displayName: payload.display_name,
    category: payload.category,
    adapterStatus: payload.adapter_status,
    defaultAuthMethod: payload.default_auth_method,
    authMethods: payload.auth_methods.map((method) => ({
      key: method.key,
      runtimeStatus: method.runtime_status,
      credentialSlots: method.credential_slots.map((slot) => ({
        key: slot.key,
        secretKind: slot.secret_kind,
        required: slot.required,
        defaultRef: slot.default_ref,
      })),
    })),
    capabilities: payload.capabilities,
  };
}

function mapProviderAccount(
  payload: ProviderAccountSummary,
): ProviderAccountFeedItem {
  return {
    id: payload.id,
    provider: payload.provider,
    accountLabel: payload.account_label,
    authMethod: payload.auth_method,
    credentialRef: payload.credential_ref,
    credentialRefs: payload.credential_refs,
    connectionMetadata: payload.connection_metadata,
    scopes: payload.scopes,
    status: payload.status,
    ownerActor: payload.owner_actor,
    credentialVersion: payload.credential_version,
    credentialRotatedAt: payload.credential_rotated_at,
    disabledAt: payload.disabled_at,
    disabledBy: payload.disabled_by,
    revokedAt: payload.revoked_at,
    revokedBy: payload.revoked_by,
    readOnly: payload.read_only,
    deployCapable: payload.deploy_capable,
    billingReadable: payload.billing_readable,
    destructiveActionsDisabled: payload.destructive_actions_disabled,
    capabilities: payload.capabilities,
    missingPermissions: payload.missing_permissions,
    lastTestStatus: payload.last_test_status,
    lastTestedAt: payload.last_tested_at,
    lastError: payload.last_error,
    createdBy: payload.created_by,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function mapResource(payload: ExternalResourceSummary): ExternalResourceFeedItem {
  return {
    id: payload.id,
    providerAccountId: payload.provider_account_id,
    provider: payload.provider,
    resourceType: payload.resource_type,
    externalId: payload.external_id,
    name: payload.name,
    state: payload.state,
    ownership: payload.ownership,
    environment: payload.environment,
    ownerRole: payload.owner_role,
    taskId: payload.task_id,
    deploymentTargetId: payload.deployment_target_id,
    receiptId: payload.receipt_id,
    budgetAllocationId: payload.budget_allocation_id,
    healthStatus: payload.health_status,
    estimatedMonthlyUsdMicros: payload.estimated_monthly_usd_micros,
    actualMonthlyUsdMicros: payload.actual_monthly_usd_micros,
    costConfidence: payload.cost_confidence,
    lastSeenAt: payload.last_seen_at,
    tags: payload.tags,
    metadata: payload.metadata,
    updatedAt: payload.updated_at,
  };
}

function mapCost(payload: CostEventSummary): CostEventFeedItem {
  return {
    id: payload.id,
    providerAccountId: payload.provider_account_id,
    externalResourceId: payload.external_resource_id,
    receiptId: payload.receipt_id,
    provider: payload.provider,
    category: payload.category,
    amountUsdMicros: payload.amount_usd_micros,
    costLayer: payload.cost_layer,
    confidence: payload.confidence,
    sourceRef: payload.source_ref,
    createdAt: payload.created_at,
  };
}

function mapCostAnomaly(payload: CostAnomalySummary): CostAnomalyFeedItem {
  return {
    id: payload.id,
    providerAccountId: payload.provider_account_id,
    costEventId: payload.cost_event_id,
    decisionId: payload.decision_id,
    provider: payload.provider,
    anomalyType: payload.anomaly_type,
    severity: payload.severity,
    status: payload.status,
    amountUsdMicros: payload.amount_usd_micros,
    thresholdUsdMicros: payload.threshold_usd_micros,
    summary: payload.summary,
    recommendedActions: payload.recommended_actions,
    createdAt: payload.created_at,
  };
}

function mapChangeSet(payload: ChangeSetSummary): ChangeSetFeedItem {
  return {
    id: payload.id,
    providerAccountId: payload.provider_account_id,
    title: payload.title,
    summary: payload.summary,
    status: payload.status,
    riskTier: payload.risk_tier,
    operations: payload.operations,
    expectedCostUsdMicros: payload.expected_cost_usd_micros,
    reservationId: payload.reservation_id,
    approvalDecisionId: payload.approval_decision_id,
    rollbackPlan: payload.rollback_plan,
    createdBy: payload.created_by,
    failureReason: payload.failure_reason,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function mapResourceReceipt(
  payload: ResourceReceiptSummary,
): ResourceReceiptFeedItem {
  return {
    id: payload.id,
    changesetId: payload.changeset_id,
    providerAccountId: payload.provider_account_id,
    dedupeKey: payload.dedupe_key,
    provider: payload.provider,
    action: payload.action,
    resourceType: payload.resource_type,
    resourceRef: payload.resource_ref,
    status: payload.status,
    providerRef: payload.provider_ref,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function mapDeploymentTarget(
  payload: DeploymentTargetSummary,
): DeploymentTargetFeedItem {
  return {
    id: payload.id,
    appName: payload.app_name,
    repo: payload.repo,
    environment: payload.environment,
    providerAccountId: payload.provider_account_id,
    targetKind: payload.target_kind,
    targetRef: payload.target_ref,
    domains: payload.domains,
    secretRefs: payload.secret_refs,
    status: payload.status,
    createdAt: payload.created_at,
  };
}

function mapDeploymentRun(payload: DeploymentRunSummary): DeploymentRunFeedItem {
  return {
    id: payload.id,
    deploymentTargetId: payload.deployment_target_id,
    changesetId: payload.changeset_id,
    decisionId: payload.decision_id,
    releaseArtifactId: payload.release_artifact_id,
    healthArtifactId: payload.health_artifact_id,
    rollbackArtifactId: payload.rollback_artifact_id,
    appName: payload.app_name,
    environment: payload.environment,
    repo: payload.repo,
    targetKind: payload.target_kind,
    targetRef: payload.target_ref,
    commitRef: payload.commit_ref,
    status: payload.status,
    rollbackPlan: payload.rollback_plan,
    healthCheckResults: payload.health_check_results,
    failureReason: payload.failure_reason,
    createdAt: payload.created_at,
  };
}

function mapDeploymentEnvironmentPolicy(
  payload: DeploymentEnvironmentPolicySummary,
): DeploymentEnvironmentPolicyFeedItem {
  return {
    id: payload.id,
    environment: payload.environment,
    runtimeMode: payload.runtime_mode,
    allowedTargetKinds: payload.allowed_target_kinds,
    requiresChangeSet: payload.requires_change_set,
    requiresBudgetReservation: payload.requires_budget_reservation,
    requiresApproval: payload.requires_approval,
    requiresHealthCheck: payload.requires_health_check,
    requiresRollbackPlan: payload.requires_rollback_plan,
    rolloutNotes: payload.rollout_notes,
    status: payload.status,
    createdAt: payload.created_at,
  };
}

export function mapDeploymentPromotion(
  payload: DeploymentPromotionSummary,
): DeploymentPromotionFeedItem {
  return {
    id: payload.id,
    appName: payload.app_name,
    repo: payload.repo,
    commitRef: payload.commit_ref,
    status: payload.status,
    pipelineStatus: String(payload.pipeline_verdict.status ?? "pending"),
    gateStatus: String(payload.gate_result.status ?? "waiting"),
    stagingRunStatus: payload.staging_deployment_run?.status,
    productionRunStatus: payload.production_deployment_run?.status,
    rolloutStatus: payload.production_rollout?.status,
    rolloutStage: payload.production_rollout?.current_stage,
    rollbackReason: payload.production_rollout?.rollback_reason,
    failureReason: payload.failure_reason,
    timeline: payload.timeline,
    createdAt: payload.created_at,
  };
}

function mapProviderHealth(payload: ProviderHealthSummary): ProviderHealthFeedItem {
  return {
    id: payload.id,
    providerAccountId: payload.provider_account_id,
    provider: payload.provider,
    status: payload.status,
    apiAvailable: payload.api_available,
    rateLimitStatus: payload.rate_limit_status,
    usageCapStatus: payload.usage_cap_status,
    lastCheckedAt: payload.last_checked_at,
  };
}

function mapDriftReport(payload: DriftReportSummary): DriftReportFeedItem {
  return {
    id: payload.id,
    providerAccountId: payload.provider_account_id,
    classification: payload.classification,
    summary: payload.summary,
    unmanagedCount: payload.unmanaged_count,
    orphanedCount: payload.orphaned_count,
    securityImpactingCount: payload.security_impacting_count,
    productionImpactingCount: payload.production_impacting_count,
    createdAt: payload.created_at,
  };
}

function mapReconciliationRun(
  payload: ReconciliationRunSummary,
): ReconciliationRunFeedItem {
  return {
    id: payload.id,
    providerAccountId: payload.provider_account_id,
    provider: payload.provider,
    status: payload.status,
    resourcesSeen: payload.resources_seen,
    resourcesChanged: payload.resources_changed,
    costsSeen: payload.costs_seen,
    startedBy: payload.started_by,
    startedAt: payload.started_at,
    finishedAt: payload.finished_at,
    inventorySnapshotArtifactId:
      typeof payload.metadata.inventory_snapshot_artifact_id === "string"
        ? payload.metadata.inventory_snapshot_artifact_id
        : undefined,
  };
}

function mapWorkerPool(payload: WorkerPoolSummary): WorkerPoolFeedItem {
  return {
    id: payload.id,
    poolKey: payload.pool_key,
    kind: payload.kind,
    providerAccountId: payload.provider_account_id,
    provider: payload.provider,
    status: payload.status,
    currentWorkers: payload.current_workers,
    desiredWorkers: payload.desired_workers,
    minWorkers: payload.min_workers,
    maxWorkers: payload.max_workers,
    maxQueueDepthPerWorker: payload.max_queue_depth_per_worker,
    costPerWorkerHourUsdMicros: payload.cost_per_worker_hour_usd_micros,
    capabilities: payload.capabilities,
    createdAt: payload.created_at,
  };
}

function mapScalePlan(payload: ScalePlanSummary): ScalePlanFeedItem {
  return {
    id: payload.id,
    workerPoolId: payload.worker_pool_id,
    status: payload.status,
    triggerReason: payload.trigger_reason,
    queueDepth: payload.queue_depth,
    desiredWorkersBefore: payload.desired_workers_before,
    desiredWorkersAfter: payload.desired_workers_after,
    targetWorkers: payload.target_workers,
    budgetState: payload.budget_state,
    budgetAvailableUsdMicros: payload.budget_available_usd_micros,
    estimatedHourlyDeltaUsdMicros: payload.estimated_hourly_delta_usd_micros,
    providerHealthStatus: payload.provider_health_status,
    actions: payload.actions,
    recommendations: payload.recommendations,
    changesetId: payload.changeset_id,
    decisionId: payload.decision_id,
    createdAt: payload.created_at,
  };
}

function summarizeProviderOps(
  feed: Omit<
    ProviderOpsFeed,
    "summary" | "apiReachable" | "blockedReason" | "partialFailures" | "refreshedAt"
  >,
): ProviderOpsSummary {
  return {
    connectedProviders: new Set(
      feed.providerAccounts.map((account) => account.provider),
    ).size,
    healthyProviders: feed.providerHealth.filter((health) => health.apiAvailable)
      .length,
    openChangeSets: feed.changeSets.filter((changeSet) =>
      ["draft", "quoted", "policy_checked", "budget_reserved", "pending_approval", "approved", "applying"].includes(
        changeSet.status,
      ),
    ).length,
    resourceCount: feed.resources.length,
    unmanagedResourceCount: feed.resources.filter(
      (resource) => resource.ownership === "discovered_unmanaged",
    ).length,
    orphanedSignalCount:
      feed.resources.filter((resource) => resource.ownership === "orphaned").length +
      feed.driftReports.reduce((total, report) => total + report.orphanedCount, 0),
    spendUsdMicros: feed.costs.reduce(
      (total, cost) => total + cost.amountUsdMicros,
      0,
    ),
    openCostAnomalies: feed.costAnomalies.filter(
      (anomaly) => anomaly.status === "open",
    ).length,
    desiredWorkerCount: feed.workerPools.reduce(
      (total, pool) => total + pool.desiredWorkers,
      0,
    ),
    openScalePlans: feed.scalePlans.filter((plan) =>
      ["planned", "pending_approval", "blocked"].includes(plan.status),
    ).length,
    activeDeploymentRuns: feed.deploymentRuns.filter((run) =>
      ["created", "building", "deploying", "rollback_pending", "rolling_back"].includes(
        run.status,
      ),
    ).length,
  };
}

export function emptyProviderOpsFeed(
  reason?: string,
  projectId = PROJECT_ID,
): ProviderOpsFeed {
  const resolvedProjectId = resolveProjectId(projectId);
  return {
    projectId: resolvedProjectId,
    apiReachable: false,
    blockedReason: reason,
    partialFailures: [],
    refreshedAt: new Date().toISOString(),
    providerDefinitions: [],
    providerAccounts: [],
    resources: [],
    costs: [],
    costAnomalies: [],
    changeSets: [],
    resourceReceipts: [],
    deploymentTargets: [],
    deploymentPolicies: [],
    deploymentRuns: [],
    deploymentPromotions: [],
    providerHealth: [],
    driftReports: [],
    reconciliationRuns: [],
    workerPools: [],
    scalePlans: [],
    summary: {
      connectedProviders: 0,
      healthyProviders: 0,
      openChangeSets: 0,
      resourceCount: 0,
      unmanagedResourceCount: 0,
      orphanedSignalCount: 0,
      spendUsdMicros: 0,
      openCostAnomalies: 0,
      desiredWorkerCount: 0,
      openScalePlans: 0,
      activeDeploymentRuns: 0,
    },
  };
}

export async function fetchProviderOpsFeed(
  projectId = PROJECT_ID,
): Promise<ProviderOpsFeed> {
  const resolvedProjectId = resolveProjectId(projectId);
  const results = await Promise.allSettled([
    client.listProviderDefinitions(),
    client.listProviderAccounts(resolvedProjectId),
    client.listExternalResources(resolvedProjectId, { limit: 50 }),
    client.listProviderCosts(resolvedProjectId, 50),
    client.listProviderCostAnomalies(resolvedProjectId, 25),
    client.listChangeSets(resolvedProjectId, undefined, 25),
    client.listResourceReceipts(resolvedProjectId, 25),
    client.listDeploymentTargets(resolvedProjectId),
    client.listDeploymentEnvironmentPolicies(resolvedProjectId),
    client.listDeploymentRuns(resolvedProjectId, 25),
    client.listDeploymentPromotions(resolvedProjectId, 25),
    client.listProviderHealth(resolvedProjectId, { limit: 25 }),
    client.listDriftReports(resolvedProjectId, 25),
    client.listReconciliationRuns(resolvedProjectId, 25),
    client.listWorkerPools(resolvedProjectId),
    client.listScalePlans(resolvedProjectId, 25),
  ] as const);

  const [
    providerDefinitionsResult,
    providerAccountsResult,
    resourcesResult,
    costsResult,
    costAnomaliesResult,
    changeSetsResult,
    resourceReceiptsResult,
    deploymentTargetsResult,
    deploymentPoliciesResult,
    deploymentRunsResult,
    deploymentPromotionsResult,
    providerHealthResult,
    driftReportsResult,
    reconciliationRunsResult,
    workerPoolsResult,
    scalePlansResult,
  ] = results;
  const partialFailures: string[] = [];
  const read = <T>(
    result: PromiseSettledResult<T>,
    label: string,
    fallback: T,
  ): T => {
    if (result.status === "fulfilled") {
      return result.value;
    }
    const reason =
      result.reason instanceof BossApiError
        ? `HTTP ${result.reason.status}`
        : result.reason instanceof Error
          ? result.reason.message
          : "unknown error";
    partialFailures.push(`${label}: ${reason}`);
    return fallback;
  };

  const providerDefinitions = read(
    providerDefinitionsResult,
    "Provider catalog",
    { items: [] },
  );
  const providerAccounts = read(
    providerAccountsResult,
    "Provider accounts",
    { project_id: resolvedProjectId, items: [] },
  );
  const resources = read(resourcesResult, "Resources", {
    project_id: resolvedProjectId,
    items: [],
    limit: 50,
    offset: 0,
  });
  const costs = read(costsResult, "Cloud costs", {
    project_id: resolvedProjectId,
    items: [],
    limit: 50,
    offset: 0,
  });
  const costAnomalies = read(costAnomaliesResult, "Cost anomalies", {
    project_id: resolvedProjectId,
    items: [],
    limit: 25,
    offset: 0,
  });
  const changeSets = read(changeSetsResult, "ChangeSets", {
    project_id: resolvedProjectId,
    items: [],
  });
  const resourceReceipts = read(resourceReceiptsResult, "Resource receipts", {
    project_id: resolvedProjectId,
    items: [],
    limit: 25,
    offset: 0,
  });
  const deploymentTargets = read(deploymentTargetsResult, "Deployment targets", {
    project_id: resolvedProjectId,
    items: [],
    limit: 25,
    offset: 0,
  });
  const deploymentPolicies = read(
    deploymentPoliciesResult,
    "Deployment policies",
    { project_id: resolvedProjectId, items: [], limit: 25, offset: 0 },
  );
  const deploymentRuns = read(deploymentRunsResult, "Deployment runs", {
    project_id: resolvedProjectId,
    items: [],
  });
  const deploymentPromotions = read(
    deploymentPromotionsResult,
    "Deployment promotions",
    { project_id: resolvedProjectId, items: [] },
  );
  const providerHealth = read(providerHealthResult, "Provider health", {
    project_id: resolvedProjectId,
    items: [],
  });
  const driftReports = read(driftReportsResult, "Drift reports", {
    project_id: resolvedProjectId,
    items: [],
  });
  const reconciliationRuns = read(
    reconciliationRunsResult,
    "Reconciliation runs",
    { project_id: resolvedProjectId, items: [] },
  );
  const workerPools = read(workerPoolsResult, "Worker pools", {
    project_id: resolvedProjectId,
    items: [],
  });
  const scalePlans = read(scalePlansResult, "Scale plans", {
    project_id: resolvedProjectId,
    items: [],
  });

  if (
    providerDefinitionsResult.status === "rejected" &&
    providerAccountsResult.status === "rejected"
  ) {
    return emptyProviderOpsFeed(
      partialFailures.join("; ") || "Provider configuration APIs unavailable",
      resolvedProjectId,
    );
  }

  try {
    const feedWithoutSummary = {
      projectId: providerAccounts.project_id,
      providerDefinitions: providerDefinitions.items.map(mapProviderDefinition),
      providerAccounts: providerAccounts.items.map(mapProviderAccount),
      resources: resources.items.map(mapResource),
      costs: costs.items.map(mapCost),
      costAnomalies: costAnomalies.items.map(mapCostAnomaly),
      changeSets: changeSets.items.map(mapChangeSet),
      resourceReceipts: resourceReceipts.items.map(mapResourceReceipt),
      deploymentTargets: deploymentTargets.items.map(mapDeploymentTarget),
      deploymentPolicies: deploymentPolicies.items.map(
        mapDeploymentEnvironmentPolicy,
      ),
      deploymentRuns: deploymentRuns.items.map(mapDeploymentRun),
      deploymentPromotions: deploymentPromotions.items.map(mapDeploymentPromotion),
      providerHealth: providerHealth.items.map(mapProviderHealth),
      driftReports: driftReports.items.map(mapDriftReport),
      reconciliationRuns: reconciliationRuns.items.map(mapReconciliationRun),
      workerPools: workerPools.items.map(mapWorkerPool),
      scalePlans: scalePlans.items.map(mapScalePlan),
    };
    return {
      ...feedWithoutSummary,
      apiReachable: true,
      partialFailures,
      refreshedAt: new Date().toISOString(),
      summary: summarizeProviderOps(feedWithoutSummary),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyProviderOpsFeed(`API returned ${error.status}`, resolvedProjectId);
    }
    return emptyProviderOpsFeed(
      error instanceof Error ? error.message : "Unknown API error",
      resolvedProjectId,
    );
  }
}
