export type HealthResponse = {
  status: string;
  database_connected: boolean;
  github_configured: boolean;
  openai_configured: boolean;
};

export type StatusResponse = {
  project_id: string;
  project_name: string;
  workspace_name: string;
  org_name: string;
  database_connected: boolean;
  api_reachable: boolean;
  budget_state: string;
  pending_decisions: number;
  open_tasks: number;
  deadletters: number;
  active_jobs: number;
  top_risks: Array<Record<string, unknown>>;
  latest_snapshot_id?: string;
  latest_snapshot_at?: string;
  blocked_reason?: string;
};

export type MetatronReference = {
  object_type: string;
  object_id: string;
  title: string;
  status?: string;
  detail?: string;
  created_at?: string;
};

export type MetatronQueryRequest = {
  project_id: string;
  question: string;
  task_id?: string;
  decision_id?: string;
  limit?: number;
};

export type MetatronQueryResponse = {
  project_id: string;
  question: string;
  category: string;
  answer: string;
  freshness_at: string;
  references: MetatronReference[];
  suggested_actions: string[];
  top_risks: Array<Record<string, unknown>>;
};

export type HealthSnapshotSummary = {
  id: string;
  project_id: string;
  generated_by: string;
  budget_state: string;
  queue_state: Record<string, unknown>;
  workload_state: Record<string, unknown>;
  incident_state: Record<string, unknown>;
  top_risks: Array<Record<string, unknown>>;
  summary: Record<string, unknown>;
  created_at: string;
};

export type HealthSnapshotListResponse = {
  project_id: string;
  items: HealthSnapshotSummary[];
};

export type HealthSnapshotRefreshRequest = {
  project_id: string;
  actor?: string;
};

export type CommandProposeRequest = {
  project_id: string;
  command: string;
  constraints?: Record<string, unknown>;
};

export type CommandProposeResponse = {
  status: string;
  task_id: string;
  job_id: string;
  artifact_id: string;
};

export type TaskSummary = {
  id: string;
  project_id: string;
  title: string;
  description?: string;
  status: string;
  priority: string;
  risk_tier: number;
  dedupe_key: string;
  created_at: string;
  updated_at: string;
};

export type TaskListResponse = {
  items: TaskSummary[];
};

export type TaskCreateRequest = {
  project_id: string;
  title: string;
  description?: string;
  priority?: string;
  risk_tier?: number;
  dedupe_key: string;
  inputs?: Record<string, unknown>;
  required_artifacts?: string[];
};

export type SelfImprovementProposalCreateRequest = {
  project_id: string;
  title: string;
  summary: string;
  change_ref: string;
  diff_summary: string;
  expected_impact?: string[];
  test_plan?: string[];
  canary_scope?: string[];
  rollback_plan?: string[];
  success_metrics?: string[];
  actor?: string;
  risk_tier?: number;
};

export type SelfImprovementProposalSummary = {
  id: string;
  project_id: string;
  task_id: string;
  title: string;
  summary: string;
  change_ref: string;
  diff_summary: string;
  status: string;
  risk_tier: number;
  expected_impact: string[];
  test_plan: string[];
  canary_scope: string[];
  rollback_plan: string[];
  success_metrics: string[];
  status_note?: string;
  proposal_artifact_id?: string;
  current_decision_id?: string;
  canary_artifact_id?: string;
  rollout_artifact_id?: string;
  rollback_artifact_id?: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type SelfImprovementProposalDetail = SelfImprovementProposalSummary;

export type SelfImprovementProposalListResponse = {
  project_id: string;
  items: SelfImprovementProposalSummary[];
};

export type SelfImprovementCanaryRequest = {
  actor?: string;
  outcome: string;
  summary: string;
  metrics?: Record<string, unknown>;
  evidence_refs?: string[];
};

export type SelfImprovementRolloutRequest = {
  actor?: string;
  summary: string;
  external_ref?: string;
  metrics?: Record<string, unknown>;
  evidence_refs?: string[];
};

export type SelfImprovementRollbackRequest = {
  actor?: string;
  reason: string;
  external_ref?: string;
  metrics?: Record<string, unknown>;
  evidence_refs?: string[];
};

export type JobSummary = {
  id: string;
  project_id: string;
  task_id?: string;
  kind: string;
  status: string;
  priority: number;
  max_attempts: number;
  attempt: number;
  run_after: string;
};

export type JobCreateRequest = {
  project_id: string;
  kind: string;
  task_id?: string;
  payload?: Record<string, unknown>;
  requires_reservation?: boolean;
  reservation_id?: string;
  priority?: number;
  max_attempts?: number;
  run_after_seconds?: number;
};

export type LeasedJob = {
  id: string;
  project_id: string;
  task_id?: string;
  kind: string;
  payload: Record<string, unknown>;
  attempt: number;
  max_attempts: number;
  lease_token: string;
  lease_expires_at: string;
};

export type WorkerPollRequest = {
  worker_id: string;
  capabilities: string[];
  max_jobs?: number;
  lease_ttl_seconds?: number;
};

export type WorkerPollResponse = {
  status: string;
  job?: LeasedJob | null;
};

export type WorkerHeartbeatRequest = {
  worker_id: string;
  job_id: string;
  lease_token: string;
  lease_ttl_seconds?: number;
};

export type WorkerHeartbeatResponse = {
  job_id: string;
  status: string;
  lease_expires_at: string;
};

export type WorkerCompleteRequest = {
  worker_id: string;
  job_id: string;
  lease_token: string;
  result?: Record<string, unknown>;
};

export type WorkerCompleteResponse = {
  job_id: string;
  status: string;
  artifact_id?: string;
  diff_artifact_id?: string;
  test_report_artifact_id?: string;
  decision_id?: string;
  receipt_id?: string;
  deployment_decision_id?: string;
  pull_request_url?: string;
  merged_sha?: string;
  branch_name?: string;
  environment?: string;
  deployment_id?: number;
  deployment_status?: string;
  deployment_url?: string;
};

export type WorkerFailRequest = {
  worker_id: string;
  job_id: string;
  lease_token: string;
  error: Record<string, unknown>;
  retryable?: boolean;
  retry_delay_seconds?: number;
};

export type WorkerFailResponse = {
  job_id: string;
  status: string;
  run_after?: string;
  deadletter_id?: string;
};

export type BudgetStatusResponse = {
  scope_type: string;
  scope_id: string;
  budget_state: string;
  cap_usd_micros: number;
  reserved_usd_micros: number;
  actual_usd_micros: number;
  available_usd_micros: number;
  committed_usd_micros: number;
  burn_approx_usd_micros: number;
  utilization_ratio: number;
  active_reservations_count: number;
  top_cost_drivers: Array<{
    skill_or_model_id: string;
    amount_usd_micros: number;
  }>;
};

export type BudgetReserveRequest = {
  project_id: string;
  category: string;
  amount_usd_micros: number;
  reason: string;
  job_id?: string;
  expires_in_minutes?: number;
};

export type BudgetReserveResponse = {
  status: string;
  reservation_id?: string;
  decision_id?: string;
  budget_state: string;
  message: string;
};

export type BudgetQuoteRequest = {
  project_id: string;
  category: string;
  amount_usd_micros: number;
  reason: string;
};

export type BudgetQuoteResponse = {
  allowed: boolean;
  category: string;
  reason: string;
  requested_usd_micros: number;
  budget_state: string;
  post_reservation_budget_state: string;
  cap_usd_micros: number;
  available_usd_micros: number;
  post_reservation_available_usd_micros: number;
};

export type BudgetReleaseRequest = {
  project_id: string;
  reservation_id: string;
  reason: string;
};

export type BudgetReleaseResponse = {
  status: string;
  reservation_id: string;
  budget_state: string;
  message: string;
};

export type DecisionCard = {
  id: string;
  task_id?: string;
  question: string;
  status: string;
  risk_tier: number;
  default_action?: string;
  recommendation: Record<string, unknown>;
  options: Array<Record<string, unknown>>;
  cost_summary: Record<string, unknown>;
  deadline_at?: string;
  approved_by?: string;
  approved_at?: string;
  delegated_to?: string;
  delegated_by?: string;
  delegated_at?: string;
  revision_requested_by?: string;
  revision_requested_at?: string;
  executed_by?: string;
  executed_at?: string;
  resolution_note?: string;
  external_ref?: string;
  created_at: string;
  updated_at: string;
};

export type DecisionInboxResponse = {
  project_id: string;
  items: DecisionCard[];
};

export type DecisionActionRequest = {
  actor?: string;
  note?: string;
  delegate_to?: string;
  external_ref?: string;
};

export type OpenAIPlanRequest = {
  project_id: string;
  reservation_id: string;
  dedupe_key: string;
  prompt: string;
  system_prompt?: string;
  created_by_job_id?: string;
};

export type OpenAIPlanResponse = {
  status: string;
  artifact_id?: string;
  receipt_id?: string;
  output_text?: string;
  decision_id?: string;
  message?: string;
};

export type FileChange = {
  path: string;
  content: string;
};

export type PullRequestProposalRequest = {
  project_id: string;
  task_id?: string;
  reservation_id: string;
  dedupe_key: string;
  title: string;
  branch_name: string;
  summary: string;
  files: FileChange[];
  created_by_job_id?: string;
};

export type PullRequestProposalJobRequest = PullRequestProposalRequest & {
  task_id?: string;
  priority?: number;
  max_attempts?: number;
};

export type PullRequestProposalResponse = {
  status: string;
  artifact_id?: string;
  diff_artifact_id?: string;
  test_report_artifact_id?: string;
  receipt_id?: string;
  decision_id?: string;
  pull_request_url?: string;
  pull_request_number?: number;
  branch_name?: string;
  validation_status?: string;
  validation_summary?: string;
  message?: string;
};

export type PullRequestMergeRequest = {
  project_id: string;
  decision_id: string;
  dedupe_key: string;
  pull_request_number: number;
  pull_request_url: string;
  title: string;
  branch_name?: string;
  created_by_job_id?: string;
};

export type PullRequestMergeJobRequest = PullRequestMergeRequest & {
  task_id?: string;
  priority?: number;
  max_attempts?: number;
};

export type PullRequestMergeResponse = {
  status: string;
  artifact_id?: string;
  receipt_id?: string;
  decision_id?: string;
  deployment_decision_id?: string;
  pull_request_url?: string;
  branch_name?: string;
  merged_sha?: string;
  message?: string;
};

export type SandboxDeploymentRequest = {
  project_id: string;
  decision_id: string;
  dedupe_key: string;
  merged_sha: string;
  pull_request_url: string;
  title: string;
  environment: string;
  branch_name?: string;
  created_by_job_id?: string;
};

export type SandboxDeploymentJobRequest = SandboxDeploymentRequest & {
  task_id?: string;
  priority?: number;
  max_attempts?: number;
};

export type SandboxDeploymentResponse = {
  status: string;
  artifact_id?: string;
  receipt_id?: string;
  decision_id?: string;
  health_decision_id?: string;
  health_verification_artifact_id?: string;
  health_verification_status?: string;
  healthcheck_url?: string;
  environment?: string;
  deployment_id?: number;
  deployment_status?: string;
  deployment_url?: string;
  merged_sha?: string;
  message?: string;
};

export type ArtifactSummary = {
  id: string;
  project_id: string;
  task_id?: string;
  artifact_type: string;
  title: string;
  uri?: string;
  content_hash?: string;
  created_by_job_id?: string;
  created_at: string;
};

export type ArtifactDetail = ArtifactSummary & {
  content: Record<string, unknown>;
};

export type ArtifactListResponse = {
  project_id: string;
  items: ArtifactSummary[];
};

export type ReceiptResponse = {
  id: string;
  project_id: string;
  dedupe_key: string;
  effect_kind: string;
  effect_scope: Record<string, unknown>;
  payload_hash?: string;
  status: string;
  provider_ref?: string;
  created_by_job_id?: string;
  result?: Record<string, unknown>;
  error?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ReceiptListResponse = {
  project_id: string;
  items: ReceiptResponse[];
};

export type BudgetReservationSummary = {
  id: string;
  project_id: string;
  job_id?: string;
  category: string;
  amount_usd_micros: number;
  status: string;
  reason?: string;
  expires_at: string;
  created_at: string;
};

export type CostReceiptSummary = {
  id: string;
  project_id: string;
  reservation_id?: string;
  receipt_id?: string;
  category: string;
  skill_or_model_id: string;
  estimated_usd_micros: number;
  actual_usd_micros?: number;
  status: string;
  created_at: string;
};

export type AuditExportScope = {
  task_id?: string;
  decision_id?: string;
  job_id?: string;
};

export type AuditExportCounts = {
  tasks: number;
  jobs: number;
  decisions: number;
  artifacts: number;
  receipts: number;
  budget_reservations: number;
  cost_receipts: number;
  routing_logs: number;
  deadletters: number;
  events: number;
};

export type AuditExportResponse = {
  project_id: string;
  project_name: string;
  workspace_name: string;
  org_name: string;
  generated_at: string;
  scope: AuditExportScope;
  counts: AuditExportCounts;
  chain: Record<string, string[]>;
  tasks: TaskSummary[];
  jobs: JobSummary[];
  decisions: DecisionCard[];
  artifacts: ArtifactSummary[];
  receipts: ReceiptResponse[];
  budget_reservations: BudgetReservationSummary[];
  cost_receipts: CostReceiptSummary[];
  routing_logs: RoutingLogItem[];
  deadletters: DeadletterItem[];
  events: EventItem[];
};

export type ReplayBundleRequest = {
  project_id: string;
  task_id?: string;
  decision_id?: string;
  job_id?: string;
  actor?: string;
};

export type ReplayBundleResponse = {
  status: string;
  artifact_id: string;
  project_id: string;
  artifact_type: string;
  created_at: string;
  scope: AuditExportScope;
  counts: AuditExportCounts;
};

export type OutboxMessageCreateRequest = {
  project_id: string;
  channel: string;
  subject?: string;
  body: string;
  dedupe_key: string;
  actor?: string;
};

export type OutboxMessageSummary = {
  id: string;
  project_id: string;
  channel: string;
  subject?: string;
  body: string;
  status: string;
  dedupe_key: string;
  receipt_id?: string;
  created_at: string;
  updated_at: string;
};

export type OutboxMessageListResponse = {
  project_id: string;
  items: OutboxMessageSummary[];
};

export type OutboxQueueRequest = {
  project_id: string;
  reservation_id: string;
  dedupe_key: string;
  actor?: string;
  priority?: number;
  max_attempts?: number;
  created_by_job_id?: string;
};

export type OutboxDeliveryResponse = {
  status: string;
  message_id?: string;
  receipt_id?: string;
  decision_id?: string;
  provider_ref?: string;
  message?: string;
};

export type InboxMessageIngestRequest = {
  project_id: string;
  channel: string;
  subject?: string;
  body: string;
  raw_ref?: string;
  actor?: string;
};

export type InboxMessageSummary = {
  id: string;
  project_id?: string;
  channel: string;
  raw_ref?: string;
  subject?: string;
  body: string;
  status: string;
  parsed: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type InboxMessageListResponse = {
  project_id: string;
  items: InboxMessageSummary[];
};

export type RoleSummary = {
  id: string;
  project_id: string;
  role_key: string;
  display_name: string;
  autonomy_level: number;
  trust_score: number;
  successful_runs: number;
  policy_violation_count: number;
  duplicate_effect_attempt_count: number;
  hidden_failure_count: number;
  low_quality_output_count: number;
  scope_violation_count: number;
  last_reviewed_at?: string;
  last_violation_at?: string;
  created_at: string;
};

export type RoleDetail = RoleSummary & {
  spec: Record<string, unknown>;
};

export type RoleListResponse = {
  project_id: string;
  items: RoleSummary[];
};

export type RoleReviewRequest = {
  project_id: string;
  actor?: string;
  summary: string;
  metrics: Record<string, unknown>;
  evidence_refs: string[];
};

export type RoleReviewSummary = {
  id: string;
  role_key: string;
  summary: string;
  metrics: Record<string, unknown>;
  evidence_refs: string[];
  trust_score_before: number;
  trust_delta: number;
  trust_score_after: number;
  autonomy_level_before: number;
  autonomy_level_after: number;
  governance_outcome: string;
  autonomy_change_id?: string;
  artifact_id?: string;
  created_by: string;
  created_at: string;
};

export type AutonomyChangeSummary = {
  id: string;
  role_key: string;
  decision_id?: string;
  change_type: string;
  trigger: string;
  from_level: number;
  to_level: number;
  status: string;
  reason: string;
  evidence_refs: string[];
  metrics: Record<string, unknown>;
  created_by: string;
  approved_by?: string;
  approved_at?: string;
  created_at: string;
  updated_at: string;
};

export type RoleGovernanceDetail = {
  project_id: string;
  role: RoleDetail;
  policy: Record<string, unknown>;
  pending_autonomy_change?: AutonomyChangeSummary;
  recent_reviews: RoleReviewSummary[];
  recent_autonomy_changes: AutonomyChangeSummary[];
};

export type PackSummary = {
  pack_id: string;
  version: string;
  description: string;
  status: string;
  requires: string[];
  roles: string[];
  workflows: string[];
  policies: string[];
  skills: string[];
  installed: boolean;
  installed_by?: string;
  installed_at?: string;
  updated_at?: string;
  installed_version?: string;
};

export type PackDetail = PackSummary & {
  role_count?: number;
  role_results: Array<Record<string, unknown>>;
  install_status?: string;
};

export type PackListResponse = {
  project_id: string;
  items: PackSummary[];
};

export type PackInstallRequest = {
  project_id: string;
  pack_id: string;
  actor?: string;
};

export type SkillSummary = {
  id: string;
  kind: string;
  risk_tier: number;
  description: string;
  mock_mode: boolean;
  cost: Record<string, unknown>;
  policy: Record<string, unknown>;
};

export type SkillListResponse = {
  items: SkillSummary[];
};

export type EntitlementSourceSummary = {
  id: string;
  project_id: string;
  provider: string;
  source_id: string;
  source_kind: string;
  entitlement_kind: string;
  owner_type: string;
  owner_id: string;
  workload_classes_allowed: string[];
  multi_tenant_allowed: boolean;
  credential_ref?: string;
  cost_mode: string;
  limit_shape: Record<string, unknown>;
  supported_models: string[];
  task_strengths: string[];
  risk_tier_max: number;
  status: string;
  effective_status: string;
  policy_notes: string[];
  observed_capacity: Record<string, unknown>;
  runtime_supported: boolean;
  created_at: string;
  updated_at: string;
};

export type EntitlementSourceListResponse = {
  project_id: string;
  items: EntitlementSourceSummary[];
};

export type EntitlementSourceUpsertRequest = {
  project_id: string;
  provider: string;
  source_id: string;
  source_kind: string;
  entitlement_kind: string;
  owner_type: string;
  owner_id: string;
  workload_classes_allowed?: string[];
  multi_tenant_allowed?: boolean;
  credential_ref?: string;
  cost_mode?: string;
  limit_shape?: Record<string, unknown>;
  supported_models?: string[];
  task_strengths?: string[];
  risk_tier_max?: number;
  status?: string;
  policy_notes?: string[];
  observed_capacity?: Record<string, unknown>;
  runtime_supported?: boolean;
  actor?: string;
};

export type RoutingPreviewRequest = {
  project_id: string;
  skill_id: string;
  task_kind: string;
  workload_class: string;
  risk_tier?: number;
  preferred_capabilities?: string[];
  budget_state?: string;
  actor?: string;
};

export type RoutingPreviewResponse = {
  route_decision_log_id: string;
  skill_id: string;
  task_kind: string;
  workload_class: string;
  risk_tier: number;
  budget_state: string;
  source_id?: string;
  provider?: string;
  model_id?: string;
  denial_reason?: string;
  rejected_sources: Array<Record<string, unknown>>;
  decision: Record<string, unknown>;
};

export type RoutingLogItem = {
  id: string;
  project_id: string;
  skill_id?: string;
  task_kind: string;
  workload_class: string;
  risk_tier: number;
  budget_state: string;
  selected_source_id?: string;
  selected_provider?: string;
  selected_model_id?: string;
  denial_reason?: string;
  rejected_sources: Array<Record<string, unknown>>;
  decision: Record<string, unknown>;
  created_at: string;
};

export type RoutingLogListResponse = {
  project_id: string;
  items: RoutingLogItem[];
};

export type DeadletterItem = {
  id: string;
  project_id?: string;
  source_kind: string;
  source_id: string;
  reason: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type DeadletterListResponse = {
  project_id: string;
  items: DeadletterItem[];
};

export type EventItem = {
  id: string;
  event_type: string;
  actor: string;
  entity_type: string;
  entity_id: string;
  data: Record<string, unknown>;
  created_at: string;
};

export type EventFeedResponse = {
  project_id: string;
  items: EventItem[];
};

export class BossApiError extends Error {
  status: number;
  body?: unknown;

  constructor(message: string, status: number, body?: unknown) {
    super(message);
    this.name = "BossApiError";
    this.status = status;
    this.body = body;
  }
}

export type BossApiClientConfig = {
  baseUrl: string;
  fetchImpl?: typeof fetch;
};

type QueryValue = string | number | boolean | undefined | null;

function withQuery(path: string, query?: Record<string, QueryValue>): string {
  if (!query) {
    return path;
  }

  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) {
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

export function createBossApiClient(config: BossApiClientConfig) {
  const baseUrl = config.baseUrl.replace(/\/$/, "");
  const fetchImpl = config.fetchImpl ?? fetch;

  async function request<T>(
    method: "GET" | "POST",
    path: string,
    options?: {
      query?: Record<string, QueryValue>;
      body?: unknown;
    },
  ): Promise<T> {
    const response = await fetchImpl(`${baseUrl}${withQuery(path, options?.query)}`, {
      method,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(options?.body ? { "Content-Type": "application/json" } : {}),
      },
      ...(options?.body ? { body: JSON.stringify(options.body) } : {}),
    });

    if (!response.ok) {
      let body: unknown;
      try {
        body = await response.json();
      } catch {
        body = await response.text();
      }
      throw new BossApiError(
        `BOSS API request failed: ${response.status}`,
        response.status,
        body,
      );
    }

    return (await response.json()) as T;
  }

  return {
    getHealth() {
      return request<HealthResponse>("GET", "/health");
    },
    getStatus(projectId: string) {
      return request<StatusResponse>("GET", "/v1/status", {
        query: { project_id: projectId },
      });
    },
    queryMetatron(payload: MetatronQueryRequest) {
      return request<MetatronQueryResponse>("POST", "/v1/metatron/query", {
        body: payload,
      });
    },
    listHealthSnapshots(projectId: string, limit = 25) {
      return request<HealthSnapshotListResponse>("GET", "/v1/health-snapshots", {
        query: { project_id: projectId, limit },
      });
    },
    refreshHealthSnapshot(payload: HealthSnapshotRefreshRequest) {
      return request<HealthSnapshotSummary>(
        "POST",
        "/v1/health-snapshots/refresh",
        { body: payload },
      );
    },
    proposeCommand(payload: CommandProposeRequest) {
      return request<CommandProposeResponse>("POST", "/v1/commands/propose", {
        body: payload,
      });
    },
    listTasks(projectId: string) {
      return request<TaskListResponse>("GET", "/v1/tasks", {
        query: { project_id: projectId },
      });
    },
    createTask(payload: TaskCreateRequest) {
      return request<TaskSummary>("POST", "/v1/tasks", {
        body: payload,
      });
    },
    createJob(payload: JobCreateRequest) {
      return request<JobSummary>("POST", "/v1/jobs", {
        body: payload,
      });
    },
    getJob(jobId: string) {
      return request<JobSummary>("GET", `/v1/jobs/${encodeURIComponent(jobId)}`);
    },
    getTask(taskId: string) {
      return request<TaskSummary>("GET", `/v1/tasks/${encodeURIComponent(taskId)}`);
    },
    listSelfImprovementProposals(projectId: string, limit = 25) {
      return request<SelfImprovementProposalListResponse>(
        "GET",
        "/v1/self-improvement/proposals",
        {
          query: { project_id: projectId, limit },
        },
      );
    },
    createSelfImprovementProposal(payload: SelfImprovementProposalCreateRequest) {
      return request<SelfImprovementProposalDetail>(
        "POST",
        "/v1/self-improvement/proposals",
        { body: payload },
      );
    },
    getSelfImprovementProposal(proposalId: string) {
      return request<SelfImprovementProposalDetail>(
        "GET",
        `/v1/self-improvement/proposals/${encodeURIComponent(proposalId)}`,
      );
    },
    recordSelfImprovementCanary(
      proposalId: string,
      payload: SelfImprovementCanaryRequest,
    ) {
      return request<SelfImprovementProposalDetail>(
        "POST",
        `/v1/self-improvement/proposals/${encodeURIComponent(proposalId)}/canary`,
        { body: payload },
      );
    },
    recordSelfImprovementRollout(
      proposalId: string,
      payload: SelfImprovementRolloutRequest,
    ) {
      return request<SelfImprovementProposalDetail>(
        "POST",
        `/v1/self-improvement/proposals/${encodeURIComponent(proposalId)}/rollout`,
        { body: payload },
      );
    },
    rollbackSelfImprovementProposal(
      proposalId: string,
      payload: SelfImprovementRollbackRequest,
    ) {
      return request<SelfImprovementProposalDetail>(
        "POST",
        `/v1/self-improvement/proposals/${encodeURIComponent(proposalId)}/rollback`,
        { body: payload },
      );
    },
    listArtifacts(projectId: string, limit = 25) {
      return request<ArtifactListResponse>("GET", "/v1/artifacts", {
        query: { project_id: projectId, limit },
      });
    },
    getArtifact(artifactId: string) {
      return request<ArtifactDetail>(
        "GET",
        `/v1/artifacts/${encodeURIComponent(artifactId)}`,
      );
    },
    listRoles(projectId: string) {
      return request<RoleListResponse>("GET", "/v1/roles", {
        query: { project_id: projectId },
      });
    },
    getRole(projectId: string, roleKey: string) {
      return request<RoleDetail>("GET", `/v1/roles/${encodeURIComponent(roleKey)}`, {
        query: { project_id: projectId },
      });
    },
    getRoleGovernance(projectId: string, roleKey: string) {
      return request<RoleGovernanceDetail>(
        "GET",
        `/v1/roles/${encodeURIComponent(roleKey)}/governance`,
        {
          query: { project_id: projectId },
        },
      );
    },
    recordRoleReview(roleKey: string, payload: RoleReviewRequest) {
      return request<RoleGovernanceDetail>(
        "POST",
        `/v1/roles/${encodeURIComponent(roleKey)}/reviews`,
        { body: payload },
      );
    },
    listPacks(projectId: string) {
      return request<PackListResponse>("GET", "/v1/packs", {
        query: { project_id: projectId },
      });
    },
    getPack(projectId: string, packId: string) {
      return request<PackDetail>("GET", `/v1/packs/${encodeURIComponent(packId)}`, {
        query: { project_id: projectId },
      });
    },
    installPack(payload: PackInstallRequest) {
      return request<PackDetail>("POST", "/v1/packs/install", {
        body: payload,
      });
    },
    listSkills() {
      return request<SkillListResponse>("GET", "/v1/skills");
    },
    listEntitlementSources(projectId: string) {
      return request<EntitlementSourceListResponse>(
        "GET",
        "/v1/entitlements/sources",
        {
          query: { project_id: projectId },
        },
      );
    },
    upsertEntitlementSource(payload: EntitlementSourceUpsertRequest) {
      return request<EntitlementSourceSummary>("POST", "/v1/entitlements/sources", {
        body: payload,
      });
    },
    previewRoute(payload: RoutingPreviewRequest) {
      return request<RoutingPreviewResponse>("POST", "/v1/router/preview", {
        body: payload,
      });
    },
    listRoutingLogs(projectId: string, limit = 25) {
      return request<RoutingLogListResponse>("GET", "/v1/router/logs", {
        query: { project_id: projectId, limit },
      });
    },
    listOutboxMessages(projectId: string, limit = 50) {
      return request<OutboxMessageListResponse>("GET", "/v1/comms/outbox", {
        query: { project_id: projectId, limit },
      });
    },
    createOutboxMessage(payload: OutboxMessageCreateRequest) {
      return request<OutboxMessageSummary>("POST", "/v1/comms/outbox", {
        body: payload,
      });
    },
    queueOutboxDelivery(messageId: string, payload: OutboxQueueRequest) {
      return request<JobSummary>(
        "POST",
        `/v1/comms/outbox/${encodeURIComponent(messageId)}/queue`,
        { body: payload },
      );
    },
    deliverOutboxMessage(messageId: string, payload: OutboxQueueRequest) {
      return request<OutboxDeliveryResponse>(
        "POST",
        `/v1/comms/outbox/${encodeURIComponent(messageId)}/deliver`,
        { body: payload },
      );
    },
    listInboxMessages(projectId: string, limit = 50) {
      return request<InboxMessageListResponse>("GET", "/v1/comms/inbox", {
        query: { project_id: projectId, limit },
      });
    },
    ingestInboxMessage(payload: InboxMessageIngestRequest) {
      return request<InboxMessageSummary>("POST", "/v1/comms/inbox/ingest", {
        body: payload,
      });
    },
    pollWorker(payload: WorkerPollRequest) {
      return request<WorkerPollResponse>("POST", "/v1/worker/poll", {
        body: payload,
      });
    },
    heartbeatWorker(payload: WorkerHeartbeatRequest) {
      return request<WorkerHeartbeatResponse>("POST", "/v1/worker/heartbeat", {
        body: payload,
      });
    },
    completeWorkerJob(payload: WorkerCompleteRequest) {
      return request<WorkerCompleteResponse>("POST", "/v1/worker/complete", {
        body: payload,
      });
    },
    failWorkerJob(payload: WorkerFailRequest) {
      return request<WorkerFailResponse>("POST", "/v1/worker/fail", {
        body: payload,
      });
    },
    getBudgetStatus(scopeType: string, scopeId: string) {
      return request<BudgetStatusResponse>("GET", "/v1/budget/status", {
        query: { scope_type: scopeType, scope_id: scopeId },
      });
    },
    quoteBudget(payload: BudgetQuoteRequest) {
      return request<BudgetQuoteResponse>("POST", "/v1/budget/quote", {
        body: payload,
      });
    },
    reserveBudget(payload: BudgetReserveRequest) {
      return request<BudgetReserveResponse>("POST", "/v1/budget/reserve", {
        body: payload,
      });
    },
    releaseBudget(payload: BudgetReleaseRequest) {
      return request<BudgetReleaseResponse>("POST", "/v1/budget/release", {
        body: payload,
      });
    },
    listDecisions(projectId: string) {
      return request<DecisionInboxResponse>("GET", "/v1/decisions/inbox", {
        query: { project_id: projectId },
      });
    },
    approveDecision(decisionId: string, payload: DecisionActionRequest = {}) {
      return request<DecisionCard>(
        "POST",
        `/v1/decisions/${encodeURIComponent(decisionId)}/approve`,
        { body: payload },
      );
    },
    rejectDecision(decisionId: string, payload: DecisionActionRequest = {}) {
      return request<DecisionCard>(
        "POST",
        `/v1/decisions/${encodeURIComponent(decisionId)}/reject`,
        { body: payload },
      );
    },
    reviseDecision(decisionId: string, payload: DecisionActionRequest = {}) {
      return request<DecisionCard>(
        "POST",
        `/v1/decisions/${encodeURIComponent(decisionId)}/revise`,
        { body: payload },
      );
    },
    delegateDecision(decisionId: string, payload: DecisionActionRequest = {}) {
      return request<DecisionCard>(
        "POST",
        `/v1/decisions/${encodeURIComponent(decisionId)}/delegate`,
        { body: payload },
      );
    },
    executeDecision(decisionId: string, payload: DecisionActionRequest = {}) {
      return request<DecisionCard>(
        "POST",
        `/v1/decisions/${encodeURIComponent(decisionId)}/execute`,
        { body: payload },
      );
    },
    createOpenAIPlan(payload: OpenAIPlanRequest) {
      return request<OpenAIPlanResponse>("POST", "/v1/openai/plans", {
        body: payload,
      });
    },
    createPullRequestProposal(payload: PullRequestProposalRequest) {
      return request<PullRequestProposalResponse>(
        "POST",
        "/v1/software-factory/proposals",
        {
          body: payload,
        },
      );
    },
    mergePullRequest(payload: PullRequestMergeRequest) {
      return request<PullRequestMergeResponse>(
        "POST",
        "/v1/software-factory/merges",
        {
          body: payload,
        },
      );
    },
    createDeployment(payload: SandboxDeploymentRequest) {
      return request<SandboxDeploymentResponse>(
        "POST",
        "/v1/software-factory/deployments",
        {
          body: payload,
        },
      );
    },
    queuePullRequestProposalJob(payload: PullRequestProposalJobRequest) {
      return request<JobSummary>("POST", "/v1/software-factory/proposals/queue", {
        body: payload,
      });
    },
    queuePullRequestMergeJob(payload: PullRequestMergeJobRequest) {
      return request<JobSummary>("POST", "/v1/software-factory/merges/queue", {
        body: payload,
      });
    },
    queueDeploymentJob(payload: SandboxDeploymentJobRequest) {
      return request<JobSummary>("POST", "/v1/software-factory/deployments/queue", {
        body: payload,
      });
    },
    getReceipt(dedupeKey: string) {
      return request<ReceiptResponse>(
        "GET",
        `/v1/receipts/${encodeURIComponent(dedupeKey)}`,
      );
    },
    listReceipts(projectId: string, limit = 50) {
      return request<ReceiptListResponse>("GET", "/v1/receipts", {
        query: { project_id: projectId, limit },
      });
    },
    exportAuditTrail(
      projectId: string,
      scope?: { taskId?: string; decisionId?: string; jobId?: string },
    ) {
      return request<AuditExportResponse>("GET", "/v1/audit/export", {
        query: {
          project_id: projectId,
          task_id: scope?.taskId,
          decision_id: scope?.decisionId,
          job_id: scope?.jobId,
        },
      });
    },
    createReplayBundle(payload: ReplayBundleRequest) {
      return request<ReplayBundleResponse>("POST", "/v1/audit/replay-bundles", {
        body: payload,
      });
    },
    listEvents(projectId: string, limit = 50) {
      return request<EventFeedResponse>("GET", "/v1/events", {
        query: { project_id: projectId, limit },
      });
    },
    listDeadletters(projectId: string, limit = 50) {
      return request<DeadletterListResponse>("GET", "/v1/deadletters", {
        query: { project_id: projectId, limit },
      });
    },
  };
}
