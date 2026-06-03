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
  blocked_reason?: string;
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
  pull_request_url?: string;
  merged_sha?: string;
  branch_name?: string;
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
};

export type OpenAIPlanRequest = {
  project_id: string;
  reservation_id: string;
  dedupe_key: string;
  prompt: string;
  system_prompt?: string;
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
  reservation_id: string;
  dedupe_key: string;
  title: string;
  branch_name: string;
  summary: string;
  files: FileChange[];
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
  pull_request_url?: string;
  branch_name?: string;
  merged_sha?: string;
  message?: string;
};

export type ReceiptResponse = {
  id: string;
  dedupe_key: string;
  effect_kind: string;
  status: string;
  provider_ref?: string;
  result?: Record<string, unknown>;
  error?: Record<string, unknown>;
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
    getReceipt(dedupeKey: string) {
      return request<ReceiptResponse>(
        "GET",
        `/v1/receipts/${encodeURIComponent(dedupeKey)}`,
      );
    },
    listEvents(projectId: string, limit = 50) {
      return request<EventFeedResponse>("GET", "/v1/events", {
        query: { project_id: projectId, limit },
      });
    },
  };
}
