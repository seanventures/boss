export type HealthResponse = {
  status: string;
  database_connected: boolean;
  github_configured: boolean;
  openai_configured: boolean;
};

export type AssuranceOverviewResponse = {
  project_id: string;
  metrics: Array<Record<string, unknown>>;
  cases: Array<Record<string, unknown>>;
  surfaces: Array<Record<string, unknown>>;
  verification_runs: Array<Record<string, unknown>>;
  settled_conclusions: Array<Record<string, unknown>>;
};

export type ModelGatewayUsageItem = {
  engine: string;
  model?: string;
  calls: number;
  successful_calls: number;
  failed_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd_micros: number;
  first_call_at?: string;
  last_call_at?: string;
};

export type ModelGatewayUsageResponse = {
  project_id: string;
  window_days: number;
  totals: Record<string, number>;
  items: ModelGatewayUsageItem[];
};

export type DataBoundary =
  | "public"
  | "internal"
  | "confidential"
  | "restricted"
  | "local-only";

export type ProjectSummary = {
  id: string;
  name: string;
  slug: string;
  workspace_id: string;
  workspace_name: string;
  org_id: string;
  org_name: string;
  created_at: string;
  /** What the project is for: `app` builds apps; `knowledge_graph` maps an existing codebase
   *  (its guided setup: GET /v1/knowledge/setup). Absent from an older API = `app`. */
  project_type?: ProjectType;
};

export type ProjectType = "app" | "knowledge_graph";

export type ProjectListResponse = {
  items: ProjectSummary[];
};

export type AuthSessionResponse = {
  auth_required: boolean;
  transport_authenticated: boolean;
  actor_binding_required: boolean;
  principal_member_id?: string;
  auth_method?: string;
  oidc_enabled: boolean;
  message: string;
};

export type SamlLoginResponse = {
  project_id: string;
  session_id: string;
  access_token: string;
  token_type: string;
  member_id: string;
  provider_id: string;
  expires_at: string;
};



export interface EditionStatusResponse {
  project_id: string;
  edition_id: string;
  display_name: string;
  status: string;
  local_auth_allowed: boolean;
  connection_mode: string;
  included_capabilities: string[];
  blocked_reason?: string;
  source_pack: string;
}































































export type EnterpriseIdentityProviderSummary = {
  id: string;
  org_id: string;
  project_id: string;
  provider_key: string;
  provider_type: string;
  display_name: string;
  issuer?: string;
  audience?: string;
  entity_id?: string;
  sso_url?: string;
  jwks_url?: string;
  certificate_fingerprint?: string;
  scim_base_url?: string;
  secret_ref?: string;
  status: string;
  test_status: string;
  blocking_gaps: string[];
  metadata: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type EnterpriseIdentityProviderListResponse = {
  project_id: string;
  items: EnterpriseIdentityProviderSummary[];
};

export type EnterpriseIdentityRolloutPolicySummary = {
  id: string;
  org_id: string;
  project_id: string;
  provider_type: string;
  runtime_mode: string;
  allowed_domains: string[];
  rollout_notes?: string;
  status: string;
  created_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type EnterpriseIdentityRolloutPolicyListResponse = {
  project_id: string;
  items: EnterpriseIdentityRolloutPolicySummary[];
};

export type EnterpriseIdentityRolloutPolicyUpsertRequest = {
  project_id: string;
  provider_type: string;
  runtime_mode?:
    | "disabled"
    | "metadata_only"
    | "auth_allowed"
    | "provisioning_allowed";
  allowed_domains?: string[];
  rollout_notes?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseIdentityProviderUpsertRequest = {
  project_id: string;
  provider_key: string;
  provider_type: string;
  display_name: string;
  issuer?: string;
  audience?: string;
  entity_id?: string;
  sso_url?: string;
  jwks_url?: string;
  certificate_fingerprint?: string;
  scim_base_url?: string;
  secret_ref?: string;
  status?: string;
  test_status?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseScimGroupSummary = {
  id: string;
  org_id: string;
  project_id: string;
  external_id?: string;
  display_name: string;
  role_keys: string[];
  permissions: string[];
  raw_group: Record<string, unknown>;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type EnterpriseScimGroupListResponse = {
  project_id: string;
  items: EnterpriseScimGroupSummary[];
};

export type EnterpriseScimGroupUpsertRequest = {
  project_id: string;
  external_id?: string;
  display_name: string;
  role_keys?: string[];
  permissions?: string[];
  raw_group?: Record<string, unknown>;
  status?: string;
  actor?: string;
};

export type EnterpriseScimUserSummary = {
  id: string;
  org_id: string;
  project_id: string;
  external_id?: string;
  user_name: string;
  display_name: string;
  member_id: string;
  role_keys: string[];
  permissions: string[];
  groups: string[];
  raw_profile: Record<string, unknown>;
  status: string;
  active: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type EnterpriseScimUserListResponse = {
  project_id: string;
  items: EnterpriseScimUserSummary[];
};

export type EnterpriseScimUserUpsertRequest = {
  project_id: string;
  external_id?: string;
  user_name: string;
  display_name: string;
  active?: boolean;
  role_keys?: string[];
  permissions?: string[];
  groups?: string[];
  raw_profile?: Record<string, unknown>;
  actor?: string;
};

export type ProjectMemberSummary = {
  id: string;
  project_id: string;
  member_id: string;
  display_name: string;
  member_type: string;
  role_keys: string[];
  permissions: string[];
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type ProjectMemberListResponse = {
  project_id: string;
  items: ProjectMemberSummary[];
};

export type ProjectMemberUpsertRequest = {
  project_id: string;
  member_id: string;
  display_name: string;
  member_type?: string;
  role_keys?: string[];
  permissions?: string[];
  actor?: string;
};

export type OrgMemberSummary = {
  id: string;
  org_id: string;
  project_id?: string;
  member_id: string;
  display_name: string;
  member_type: string;
  role_keys: string[];
  permissions: string[];
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type OrgMemberListResponse = {
  org_id: string;
  items: OrgMemberSummary[];
};

export type OrgMemberUpsertRequest = {
  org_id: string;
  project_id: string;
  member_id: string;
  display_name: string;
  member_type?: string;
  role_keys?: string[];
  permissions?: string[];
  actor?: string;
};

export type BreakGlassGrantSummary = {
  id: string;
  project_id: string;
  target_member_id: string;
  requested_by: string;
  reason: string;
  scope: Record<string, unknown>;
  granted_permissions: string[];
  status: string;
  expires_at: string;
  postmortem_task_id?: string;
  revoked_by?: string;
  revoked_reason?: string;
  revoked_at?: string;
  created_at: string;
  updated_at: string;
};

export type BreakGlassGrantListResponse = {
  project_id: string;
  items: BreakGlassGrantSummary[];
};

export type BreakGlassGrantCreateRequest = {
  project_id: string;
  target_member_id: string;
  reason: string;
  scope?: Record<string, unknown>;
  granted_permissions: string[];
  ttl_minutes?: number;
  actor?: string;
};

export type BreakGlassGrantRevokeRequest = {
  actor?: string;
  reason: string;
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
  actor?: string;
};

export type CommandProposeResponse = {
  status: string;
  task_id: string;
  job_id: string;
  artifact_id: string;
  plan_id?: string;
  phases?: Array<Record<string, unknown>>;
};

export type AgentAction = {
  kind: string;
  task_id?: string;
  run_id?: string;
  meeting_id?: string;
  plan_id?: string;
  phase_count?: number;
  phases?: Array<Record<string, unknown>>;
  requires_confirmation?: boolean;
  href?: string;
};

export type AgentPendingConfirmation = {
  plan_id: string;
  summary: string;
  phase_count: number;
  phases: Array<Record<string, unknown>>;
};

export type AgentMessage = {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_calls: Array<Record<string, unknown>>;
  actions: AgentAction[];
  created_at: string;
};

export type AgentChatRequest = {
  project_id: string;
  actor?: string;
  thread_id?: string;
  message?: string;
  confirm_plan_id?: string;
  surface?: "chat" | "github";
  external_ref?: string;
};

export type AgentChatResponse = {
  thread_id: string;
  reply: string;
  actions: AgentAction[];
  pending_confirmation?: AgentPendingConfirmation | null;
  messages: AgentMessage[];
};

export type TaskSummary = {
  id: string;
  project_id: string;
  title: string;
  description?: string;
  status: string;
  /** BOSS-281: activity-derived lifecycle (latest job + pending decisions). */
  derived_status?: string;
  owner_role_id?: string;
  parent_task_id?: string;
  dependency_task_ids: string[];
  due_at?: string;
  priority: string;
  risk_tier: number;
  data_boundary: DataBoundary;
  dedupe_key: string;
  created_at: string;
  updated_at: string;
};

export type TaskListResponse = {
  project_id: string;
  items: TaskSummary[];
  limit: number;
  offset: number;
};

export type TaskCreateRequest = {
  project_id: string;
  title: string;
  description?: string;
  owner_role_id?: string;
  parent_task_id?: string;
  dependency_task_ids?: string[];
  due_at?: string;
  priority?: string;
  risk_tier?: number;
  data_boundary?: DataBoundary;
  dedupe_key: string;
  inputs?: Record<string, unknown>;
  required_artifacts?: string[];
  actor?: string;
};

export type DepartmentSummary = {
  id: string;
  project_id: string;
  department_key: string;
  name: string;
  mission: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type DepartmentCreateRequest = {
  project_id: string;
  department_key: string;
  name: string;
  mission: string;
  actor?: string;
};

export type DepartmentListResponse = {
  project_id: string;
  items: DepartmentSummary[];
};

export type KpiSummary = {
  id: string;
  project_id: string;
  kpi_key: string;
  name: string;
  description?: string;
  owner_role_id?: string;
  target_value: number;
  current_value: number;
  unit: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type KpiCreateRequest = {
  project_id: string;
  kpi_key: string;
  name: string;
  description?: string;
  target_value: number;
  current_value?: number;
  unit?: string;
  owner_role_id?: string;
  actor?: string;
};

export type KpiListResponse = {
  project_id: string;
  items: KpiSummary[];
  limit: number;
  offset: number;
};

export type PolicyRecordSummary = {
  id: string;
  project_id: string;
  policy_key: string;
  version: number;
  title: string;
  status: string;
  body: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type PolicyRecordCreateRequest = {
  project_id: string;
  policy_key: string;
  version?: number;
  title: string;
  body: Record<string, unknown>;
  actor?: string;
};

export type PolicyRecordListResponse = {
  project_id: string;
  items: PolicyRecordSummary[];
};

export type PlanSummary = {
  id: string;
  project_id: string;
  task_id?: string;
  title: string;
  objective: string;
  status: string;
  phases: unknown[];
  success_criteria: unknown[];
  risks: unknown[];
  artifact_id?: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type PlanCreateRequest = {
  project_id: string;
  title: string;
  objective: string;
  task_id?: string;
  phases?: unknown[];
  success_criteria?: unknown[];
  risks?: unknown[];
  artifact_id?: string;
  actor?: string;
};

export type PlanListResponse = {
  project_id: string;
  items: PlanSummary[];
};

export type CodeTaskTestResult = {
  command: string;
  exit_code: number | null;
  timed_out?: boolean;
  duration_ms?: number;
};

export type CodeTaskState = {
  engine?: string;
  summary?: string;
  files_changed?: number;
  additions?: number;
  deletions?: number;
  paths?: string[];
  tests_passed?: boolean;
  tests?: CodeTaskTestResult[];
  cost_usd_micros?: number;
  turns?: number;
  // Governed-pipeline evidence (Strict/Balanced): review + quality-gate detail
  profile?: string;
  risk_tier?: number;
  model?: string;
  build_engine?: string;
  review_engine?: string;
  review_model?: string;
  qa_summary?: string;
  impl_summary?: string;
  review_summary?: string;
  security_summary?: string;
  consistency_summary?: string;
  security_verdict?: string;
  adversarial_verdict?: string;
  consistency_verdict?: string;
  regressions_ok?: boolean;
  mutation_caught?: boolean;
  reviewer_independent?: boolean;
  test_author_independent?: boolean;
  test_exit_code?: number;
  test_command?: string;
  impl_files?: string[];
  test_files?: string[];
  substrate?: string;
  sandbox_task_ids?: string[];
};

export type CodeTaskRun = {
  id: string;
  project_id: string;
  task_id?: string | null;
  status: string; // running | completed | failed | blocked
  phase: string;  // queued|provisioning|coding|validating|opening_pr|done
  title: string;
  spec: {
    instructions?: string;
    acceptance_criteria?: string[];
    test_commands?: string[];
    owner?: string;
    repo?: string;
    base_branch?: string;
  };
  /** The API has always returned these; the type simply did not declare them, so the console
   *  could not show WHEN anything happened and every task looked equally current. */
  created_at?: string;
  updated_at?: string;
  state: CodeTaskState;
  usage?: { cost_usd_micros?: number; model?: string };
  finalize_ctx?: {
    execution_substrate?: string;
    sandbox_task_id?: string;
    sandbox_task_ids?: string[];
    root_run_id?: string;
    attempt?: number;
    /** Gates the run failed. Three paths record them under different keys: `failing_gates`
     *  when parked for override, `advisory_failing_gates` when an advisory profile shipped
     *  the PR with findings (the normal case), and dissent.unresolved_gates when repair
     *  rounds were spent. The retry uses these to decide which stages are actually stale. */
    failing_gates?: string[];
    advisory_failing_gates?: string[];
    qa_benchmark?: {
      id: string;
      name: string;
      verification_commands: string[];
      completion_contract: string[];
    };
  };
  reservation_id?: string | null;
  pull_request_url?: string | null;
  pull_request_number?: number | null;
  decision_id?: string | null;
  error?: string | null;
};

export type CodeTasksResponse = {
  code_tasks: CodeTaskRun[];
  /** How many runs match the same filter, so a caller can tell a full window from a page.
   *  Optional: an older API that does not send it leaves the count as-is rather than
   *  reporting zero, which would read as "no tasks" on a list that plainly has some. */
  total?: number;
  limit?: number;
  truncated?: boolean;
};

export type QaBenchmarkApp = {
  id: string;
  name: string;
  tagline: string;
  summary: string;
  focus: string[];
  acceptance_scenarios: string[];
  completion_contract: string[];
  verification_commands: string[];
  scoring_dimensions: Array<{
    id: string;
    label: string;
    weight: number;
    evidence: string;
  }>;
  score_baselines: Record<string, number>;
  /** Present when this benchmark has already been built in the project being viewed, so the
   *  card can offer the existing app rather than inviting a second, identical build. */
  built?: {
    run_id: string;
    run_status?: string;
    reportable?: boolean;
    pull_request_url?: string | null;
    /** A live preview of the built app, when one is running. "View" should go HERE: a run
     *  page shows a pipeline, and someone asking to see the app means the app. */
    preview_id?: string | null;
    preview_url?: string | null;
    /** Which signal identified this as the benchmark's build; `name` is the weakest. */
    matched_on?: string;
  };
};

export type QaBenchmarkAppsResponse = { benchmark_apps: QaBenchmarkApp[] };

export type QaBenchmarkReport = {
  run_id: string;
  benchmark_id?: string;
  benchmark_name?: string;
  run_status: string;
  completion_contract: string[];
  required_verification_commands: string[];
  results: Array<{
    label: string;
    command: string;
    status: "pass" | "fail" | "not_recorded";
    exit_code?: number | null;
    timed_out?: boolean | null;
  }>;
  passed: boolean;
  honesty?: {
    honest?: boolean;
    scored?: string[];
    missing_evidence?: Array<{ id?: string; reason?: string }>;
    invented_scores?: unknown[];
  };
  ui?: { status?: string } | string;
  ux?: { status?: string } | string;
  scorecard: {
    version: string;
    baselines: Record<string, number>;
    dimensions: Array<{
      id: string;
      label: string;
      weight: number;
      evidence: string;
      status: "scored" | "not_scored";
      score: number | null;
      reason?: string;
    }>;
    observed_weight: number;
    evidence_coverage_pct: number;
    overall_score: number | null;
    overall_status: "provisional" | "complete";
    tuning_note: string;
  };
  message: string;
};

export type QaBenchmarkTuningProposalRequest = {
  project_id: string;
  /** A developer-authored PR or configuration-diff reference. */
  change_ref: string;
  diff_summary: string;
  actor?: string;
  risk_tier?: number;
};

export type QaBenchmarkEvaluation = {
  id: string;
  project_id: string;
  code_task_run_id: string;
  benchmark_id: string;
  scorecard_version: string;
  status: "ready_for_candidate" | "not_ready";
  overall_score: number | null;
  observed_weight: number;
  recommendation?: Record<string, unknown>;
  evaluated_by: string;
  created_at: string;
  updated_at: string;
};

// One per-stage model call recorded for a code task (GET /v1/code-tasks/{id}/activity) —
// the internal detail behind each phase: model, turns, latency, retries, cost, ok/error.
export type LlmCallRow = {
  stage: string;
  engine: string;
  model: string;
  /** Which agent owns this stage. Persisted per call and resolved to the role the registry
   *  actually holds, so a call written under a retired name still names a real agent. */
  role: string | null;
  total_tokens: number;
  cost_usd_micros: number;
  latency_ms: number;
  retries: number;
  turns: number;
  ok: boolean;
  error: string | null;
  created_at: string | null;
};

export type CodeTaskActivityResponse = { run_id: string; calls: LlmCallRow[] };

export type PreviewRec = {
  id: string;
  run_id?: string | null;
  status: string; // starting | running | stopped | failed | expired
  /** What the preview has done and what is still pending — the same ledger a deployment
   *  records. A preview clones, installs, builds and launches; without this it is a spinner
   *  with no detail, and a failure cannot say how far it got. */
  steps?: {
    step: string; label?: string; status: string;
    started_at?: string | null; ended_at?: string | null;
    duration_ms?: number | null; log?: string;
  }[] | null;
  /** Operator-reachable gateway URL. Relative `/v1/previews/{id}/web/` or
   * `{BOSS_PREVIEW_PUBLIC_BASE}/v1/previews/{id}/web/` when that origin is set.
   * Never loopback. */
  url?: string | null;
  /** Same as `url` — explicit name for the address a person can open. */
  open_url?: string | null;
  /** Sandbox-internal bind (e.g. http://127.0.0.1:PORT); API proxy only. */
  internal_url?: string | null;
  mode?: string | null;
  substrate?: string | null;
  build_substrate?: string | null;
  sandbox_task_id?: string | null;
  failure_kind?: string | null;
  branch?: string | null;
  repo?: string | null;
  error?: string | null;
  expires_at?: string | null;
  /** The app's demo logins this preview signs in with (names only, e.g. DEMO_PASSWORD): BOSS
   *  generated the password; the reviewer reads it in the app's Setup › Demo logins. */
  demo_logins?: string[] | null;
};

export type FeedbackProposal = {
  source_run_id: string;
  classification: "scoped_tweak" | "full_rerun";
  fix_note: string;
  profile: string;
  repair_retries: number;
};

export type FeedbackResponse = {
  thread_id: string;
  status: string;
  question?: string;
  requires_confirmation?: boolean;
  proposal?: FeedbackProposal;
  dispatched?: boolean;
  run_id?: string;
};

// --- Discovery pipeline (research-driven outcome discovery) ----------------
export type DiscoveryFinding = {
  angle?: string;
  finding?: string;
  evidence?: string;
  citation_url?: string;
};
export type DiscoveryOutcome = {
  outcome?: string;
  wedge?: string;
  target_pilot?: string;
  first_prototype_slice?: string;
  validation_plan?: string;
  rationale?: string;
  confidence_0_1?: number;
  supporting_signals?: string[];
};
export type DiscoveryDossier = {
  research_brief?: { hypothesis?: string; questions?: string[]; sources_consulted?: string[] };
  signal_findings?: DiscoveryFinding[];
  market_analysis?: Record<string, unknown>;
  critique?: {
    claims?: { claim?: string; verdict?: string; reason?: string }[];
    disconfirming_evidence?: string[];
    overall_confidence_0_1?: number;
    biggest_risk?: string;
  };
  candidate_outcomes?: DiscoveryOutcome[];
  gate?: {
    verdict?: string; // pursue | promising | needs_more_research
    checks?: Record<string, boolean>;
    confidence?: number;
    thesis_confidence?: number;
    best_outcome_confidence?: number;
    cited_sources?: number;
  };
  promotions?: { outcome_index: number; task_id: string; meeting_run_id: string }[];
};
export type DiscoveryRun = {
  id: string;
  project_id: string;
  status: string; // running | completed | failed
  phase: string;  // queued|signal_sweep|synthesize|vet|shape|gate|done
  title: string;
  seed?: string | null;
  context?: string | null;
  state: DiscoveryDossier;
  usage?: { cost_usd_micros?: number; model?: string } | null;
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /** Why the run is not moving, in words an operator can act on; null when it is fine. */
  attention?: ModelAttention | null;
};
/** What a LIST row of discoveries carries: GET /v1/discoveries. The dossier (`state`) is the
 *  DETAIL — GET /v1/discoveries/{id} — fetched when one is opened. */
export type DiscoverySummary = {
  verdict?: string | null;
  confidence?: number | null;
  findings: number;
  outcomes: number;
  deep?: boolean;
};
export type DiscoveryListItem = Omit<DiscoveryRun, "state" | "context"> & { summary: DiscoverySummary };
/** The project's true totals (every dossier, not only the listed page). */
export type DiscoveryTotals = { dossiers: number; promising: number; needs_more: number; not_worth: number;
                               signals: number; outcomes: number };
export type DiscoveriesResponse = { discoveries: DiscoveryListItem[]; totals?: DiscoveryTotals };

/** engine/boss_engine/services/model_failures.py `explain`, plus where the work stands. */
export type ModelAttention = {
  kind: string;          // login_rejected | budget_hold | budget_wait | budget_refused | no_model | ...
  state?: string;        // stopped | waiting | retrying
  headline: string;
  action: string;
  href?: string | null;
  retry?: boolean;
  detail?: string;
};

export type SandboxSummary = {
  sandbox_id: string;
  tenant_id: string;
  repo: string;
  title: string;
  status: string; // running | idle | failed
  container_id: string;
  created_at: number;
  updated_at: number;
  age_seconds: number;
  meta?: {
    engine?: string;
    summary?: string;
    diff_summary?: { files_changed?: number; additions?: number; deletions?: number; paths?: string[] };
    tests?: { command: string; exit_code: number | null }[];
    tests_passed?: boolean;
  };
  preserved?: boolean;
  preserve_reason?: string;
  preserved_by?: string;
  snapshot_image?: string;
};

export type SandboxHost = {
  connected: boolean;
  kind?: string;
  capacity?: number;
  available?: number;
  running?: number;
  url?: string;
};
export type SandboxesResponse = { sandboxes: SandboxSummary[]; unavailable?: string; host?: SandboxHost };

export type SandboxStats = {
  sandbox_id: string;
  status: string;
  cpu_pct?: string;
  mem_usage?: string;
  mem_pct?: string;
  net_io?: string;
  block_io?: string;
  pids?: string;
  oom_killed?: boolean;
  exit_code?: number;
  container_status?: string;
  mem_limit_bytes?: number;
  nano_cpus?: number;
  restart_count?: number;
};

export type SandboxExecResult = {
  exit_code?: number;
  stdout?: string;
  stderr?: string;
  break_glass?: boolean;
  blocked?: boolean;
  detail?: string;
};

export type CodeTaskCreatePayload = {
  project_id: string;
  title: string;
  instructions: string;
  acceptance_criteria?: string[];
  test_commands?: string[];
  owner?: string;
  repo?: string;
  base_branch?: string;
  task_id?: string;
  // The API has always accepted these; the client type omitted them, so callers that meant
  // to reproduce a run's cost/rigor silently got the project default instead. Re-running a
  // `lean` task as whatever-comes-next is not a re-run.
  profile?: string;
  /** Reviewers on the code-review panel (each seat signs its own work). */
  review_panel_size?: number;
  risk_tier?: number;
  /** The workflow version this project should follow. Picked by the operator; outranks any
   *  mapping resolved on their behalf. Omit to let the project's mapping (or the standard
   *  workflow) decide. */
  workflow_version_id?: string;
};

export type MeetingSummary = {
  id: string;
  project_id: string;
  title: string;
  purpose: string;
  status: string;
  participants: string[];
  agenda: string[];
  transcript_artifact_id?: string;
  minutes_artifact_id?: string;
  action_task_ids: string[];
  decisions: unknown[];
  dissent_records: unknown[];
  blocked_state?: Record<string, unknown> | null;
  completed_at?: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type MeetingCreateRequest = {
  project_id: string;
  title: string;
  purpose: string;
  participants?: string[];
  agenda?: string[];
  actor?: string;
};

export type MeetingRunRequest = {
  actor?: string;
  outcome_status?: "completed" | "blocked";
  transcript: Array<{
    speaker: string;
    message: string;
    role_id?: string;
    phase?: string;
    timestamp?: string;
    references?: string[];
  }>;
  minutes: Record<string, unknown>;
  action_items?: Array<{
    title: string;
    description?: string;
    summary?: string;
    owner_role_id?: string;
    due_at?: string;
    priority?: string;
    risk_tier?: number;
    data_boundary?: DataBoundary;
  }>;
  decision_items?: Array<{
    question: string;
    recommendation?: Record<string, unknown> | string;
    options?: Array<Record<string, unknown>>;
    risk_tier?: number;
    cost_summary?: Record<string, unknown>;
    deadline_at?: string;
    default_action?: string;
    task_id?: string;
  }>;
  consensus_reached?: boolean;
  dissent_records?: Array<Record<string, unknown>>;
  blocked_state?: Record<string, unknown>;
};

export type MeetingListResponse = {
  project_id: string;
  items: MeetingSummary[];
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
  limit: number;
  offset: number;
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
  data_boundary: DataBoundary;
  priority: number;
  max_attempts: number;
  attempt: number;
  run_after: string;
};

export type JobListItem = JobSummary & {
  task_title?: string;
  lease_owner?: string;
  lease_expires_at?: string;
  heartbeat_at?: string;
  last_error?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type JobListResponse = {
  project_id: string;
  items: JobListItem[];
  limit: number;
  offset: number;
};

export type JobCreateRequest = {
  project_id: string;
  kind: string;
  task_id?: string;
  payload?: Record<string, unknown>;
  data_boundary?: DataBoundary;
  requires_reservation?: boolean;
  reservation_id?: string;
  priority?: number;
  max_attempts?: number;
  run_after_seconds?: number;
};

export type OpsQueueJobItem = {
  id: string;
  project_id: string;
  task_id?: string;
  task_title?: string;
  kind: string;
  status: string;
  priority: number;
  attempt: number;
  max_attempts: number;
  data_boundary: DataBoundary;
  run_after: string;
  lease_owner?: string;
  lease_expires_at?: string;
  heartbeat_at?: string;
  last_error: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type OpsQueueDecisionItem = {
  id: string;
  task_id?: string;
  question: string;
  status: string;
  risk_tier: number;
  default_action?: string;
  deadline_at?: string;
  created_at: string;
  updated_at: string;
};

export type OpsQueueInspectionResponse = {
  project_id: string;
  generated_at: string;
  budget_state: string;
  queue_state: Record<string, unknown>;
  workload_state: Record<string, unknown>;
  incident_state: Record<string, unknown>;
  job_status_counts: Record<string, number>;
  job_kind_counts: Record<string, number>;
  stale_lease_job_ids: string[];
  ready_job_ids: string[];
  jobs: OpsQueueJobItem[];
  deadletters: DeadletterItem[];
  pending_decisions: OpsQueueDecisionItem[];
  blockers: Array<Record<string, unknown>>;
  recommended_actions: string[];
};

export type OpsDigestRequest = {
  project_id: string;
  actor?: string;
  limit?: number;
  include_terminal?: boolean;
};

export type OpsDigestResponse = {
  status: string;
  project_id: string;
  artifact_id: string;
  artifact_type: string;
  created_at: string;
  counts: Record<string, number>;
  queue_state: Record<string, unknown>;
  blockers: Array<Record<string, unknown>>;
  recommended_actions: string[];
};

export type LeasedJob = {
  id: string;
  project_id: string;
  task_id?: string;
  kind: string;
  payload: Record<string, unknown>;
  data_boundary: DataBoundary;
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
  security_review_artifact_id?: string;
  release_go_no_go_artifact_id?: string;
  decision_id?: string;
  receipt_id?: string;
  deployment_decision_id?: string;
  pull_request_url?: string;
  merged_sha?: string;
  branch_name?: string;
  validation_status?: string;
  validation_summary?: string;
  validation_sources?: string[];
  local_validation_receipt_id?: string;
  environment?: string;
  deployment_id?: number;
  deployment_status?: string;
  deployment_url?: string;
  rollout_id?: string;
  rollout_artifact_id?: string;
  rollout_status?: string;
  rollout_stage?: string;
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

export type McpServer = {
  id: string;
  name: string;
  url: string;
  transport: string;
  risk_tier: number;
  data_boundary: DataBoundary;
  enabled: boolean;
  description?: string;
  created_at: string;
  updated_at?: string | null;
  /** A person's review, recorded with who gave it (provenance.reviewed_by). */
  trusted?: boolean;
  provenance?: Record<string, unknown>;
  tool_risk_overrides?: Record<string, number>;
  /** What the last probe found. null = never checked, which is not the same as down. */
  reachable?: boolean | null;
  last_checked_at?: string | null;
  last_error?: string | null;
  /** One sentence on what is wrong and what to do, or null when nothing is. */
  status_note?: string | null;
  /** The last-known tools — kept across a failed probe. */
  tools?: { name: string; description?: string; inputSchema?: unknown }[];
  /** What calls this server: what a delete would break. */
  references?: {
    custom_steps: { step_key: string; label?: string; tool?: string }[];
    skills: { skill_key: string; label?: string; tool?: string }[];
  };
};

/** An edit to a registered server. Only the fields sent change; the id stays. */
export type McpServerUpdatePayload = {
  name?: string;
  url?: string;
  transport?: string;
  description?: string;
  risk_tier?: number;
  data_boundary?: DataBoundary;
  tool_risk_overrides?: Record<string, number>;
  enabled?: boolean;
  trusted?: boolean;
};

export type McpServersResponse = {
  project_id: string;
  items: McpServer[];
};

export type NativeToolParam = {
  name: string;
  type: string;
  required: boolean;
  description?: string;
};

export type NativeTool = {
  name: string;
  kind: "read" | "write";
  risk_tier: number;
  description: string;
  parameters: NativeToolParam[];
};

export type McpServerTool = {
  name: string;
  description?: string;
};

export type McpServerWithTools = McpServer & {
  reachable: boolean;
  tools: McpServerTool[];
  error?: string;
};

export type ApprovalToolRule = {
  name: string;
  source: string;
  kind: string;
  risk_tier: number;
  requires_approval: boolean;
};
export type ApprovalServiceGate = { domain: string; gate: string; trigger: string; reason: string };
export type ApprovalRulesResponse = {
  project_id: string;
  autonomy_threshold: number;
  autonomy_explainer: string;
  tool_rules: ApprovalToolRule[];
  service_gates: ApprovalServiceGate[];
  budget_gates: Record<string, number>;
  policies: { policy_key: string; title: string; status: string; version: number }[];
};

export type CapabilitiesResponse = {
  project_id: string;
  engine?: string;
  engine_label?: string;
  engine_model?: string;
  native_tools: NativeTool[];
  skills: SkillSummary[];
  mcp_servers: McpServerWithTools[];
};

export type OrgSummary = {
  org_id: string;
  org_name: string;
  created_at: string;
  project_id: string;
  project_name: string;
  project_slug: string;
  workspace_name: string;
  task_count: number;
  meeting_count: number;
};

export type OrgsResponse = { items: OrgSummary[] };

export type MeetingRole = {
  key: string;
  title: string;
  lens: string;
  default: boolean;
  is_default?: boolean;
  enabled?: boolean;
  risk_tier?: number;
  data_boundary?: string;
};

export type MeetingRolesResponse = {
  roles: MeetingRole[];
  default_role_keys: string[];
};

export type Meeting = {
  id: string;
  task_id?: string | null;
  task_title?: string | null;
  task_status?: string | null;
  role_keys?: string[];
  title: string;
  purpose: string;
  status: string;
  phase?: string;
  participants: string[];
  created_at: string;
  completed_at?: string | null;
  directive?: string | null;
  url?: string | null;
  model?: string | null;
  cost_usd_micros?: number | null;
  research_summary?: { sitemap?: string[]; pages_studied?: number };
  round1: Record<string, string>;
  round2: Record<string, string>;
  /** Each seat's headline and up to three key points, per round — the seat's own, or cut from
   *  its reply when it wrote none (engine meeting_gist). Keyed like `round1`/`round2`. */
  round1_gists?: Record<string, MeetingTurnGist>;
  round2_gists?: Record<string, MeetingTurnGist>;
  /** The facilitator's synthesis as a headline and up to five points. */
  synthesis_gist?: MeetingTurnGist | null;
  synthesis?: string | null;
  prd_markdown?: string | null;
  pull_request_url?: string | null;
  pull_request_number?: number | null;
  /** Where the meeting's PRD stands, from the decision that gates it (meeting_service.prd_state_of). */
  prd_state?: string | null;
  /** The same, in the operator's words. Rendered as given; the console does not re-derive it. */
  prd_label?: string | null;
  /** What the person asked, apart from the prompt scaffolding (engine meeting_brief). */
  brief?: string | null;
  /** What BOSS added to the prompt (the workflow's purpose, the research), as markdown. */
  context?: string | null;
  meeting_type?: "prd_panel" | "go_no_go" | "standup" | string | null;
  /** A security review's panel: the review run whose checklist this panel's document became. */
  security_review?: { run_id: string | null } | null;
  /** Why a failed panel stopped. */
  error?: string | null;
  /** The decision that gates publishing the PRD. */
  decision_id?: string | null;
  /** Whether minutes exist to record a vote against. */
  can_vote?: boolean;
  /** The build the PRD fed, once one started. */
  build?: { id: string; status: string; phase?: string | null; pull_request_url?: string | null } | null;
  /** go/no-go: the verdict; typed meetings: the dissent and actions they recorded. */
  verdict?: "go" | "no_go" | null;
  dissent?: Array<{ role?: string | null; position?: string | null; reason?: string | null }>;
  action_items?: Array<{ title: string; description?: string | null }>;
};

/** A panel turn in brief: plain text, never markdown. */
export type MeetingTurnGist = { headline: string; points: string[] };

export type MeetingsResponse = {
  project_id: string;
  items: Meeting[];
  /** The project's true totals (every panel, not only the listed page). */
  totals?: { panels: number; prds: number };
};

export type MeetingConvenePayload = {
  project_id: string;
  directive: string;
  url?: string;
  roles?: string[];
  task_id?: string;
  constraints?: Record<string, unknown>;
  actor?: string;
};

export type McpServerRegisterPayload = {
  project_id: string;
  name: string;
  url: string;
  transport?: string;
  risk_tier?: number;
  data_boundary?: DataBoundary;
  description?: string;
  actor?: string;
};

export type AgentRunStep = {
  step: number;
  tool: string;
  args: Record<string, unknown>;
  result_preview: string;
  deferred?: boolean;
  resumed?: boolean;
  thought?: string;
};

export type ConversationToolCall = {
  name: string;
  args: Record<string, unknown>;
};

export type ConversationTurn = {
  role: "system" | "user" | "assistant" | "tool";
  text: string;
  name?: string;
  calls?: ConversationToolCall[];
};

export type AgentRun = {
  artifact_id: string;
  task_id?: string;
  created_at: string;
  model?: string;
  tool_calls: number;
  cost_usd_micros: number;
  total_tokens: number;
  transcript: AgentRunStep[];
  output_text?: string | null;
  status?: string | null;
  conversation?: ConversationTurn[];
};

export type AgentRunsResponse = {
  project_id: string;
  items: AgentRun[];
};

export type TaskOutput = {
  task_id: string;
  kind: string;
  title: string;
  link_kind: "external" | "artifact" | "task";
  url?: string | null;
  artifact_id?: string;
  ref_task_id?: string;
  status?: string;
  preview?: string | null;
  created_at: string;
};

export type TaskOutputsResponse = {
  project_id: string;
  items: TaskOutput[];
};

export type ReportWindow = {
  tasks: number;
  jobs: number;
  jobs_succeeded: number;
  jobs_failed: number;
  decisions: number;
  decisions_approved: number;
  spend_usd_micros: number;
};

export type ReportSummaryResponse = {
  project_id: string;
  generated_at: string;
  windows: { today: ReportWindow; week: ReportWindow; month: ReportWindow };
  daily: Array<{ date: string; tasks: number; succeeded: number; failed: number }>;
  by_kind: Array<{
    kind: string;
    total: number;
    succeeded: number;
    failed: number;
    avg_seconds: number;
  }>;
  by_model: Array<{ model: string; count: number; spend_usd_micros: number }>;
  agents: Array<{
    role_key: string;
    display_name: string;
    trust_score: number;
    autonomy_level: number;
    successful_runs: number;
    policy_violations: number;
    scope_violations: number;
    last_reviewed_at: string | null;
    last_violation_at: string | null;
  }>;
  decision_latency: { count: number; avg_seconds: number };
};

/** One unresolved historical model call; the original estimate is not an invoice or a total. */
export type LegacyFinancialHold = {
  reservation_id: string;
  lease_id: string;
  invocation_id: string;
  receipt_id: string;
  original_reservation_status: "expired" | "released";
  original_estimate_usd_micros: number;
  decision_id: string | null;
  message: string;
};

/** One unresolved historical provider attempt; the original estimate is not an invoice or a total. */
export type LegacyProviderHold = {
  reservation_id: string;
  changeset_id: string;
  operation_id: string;
  resource_receipt_id: string;
  receipt_id: string;
  original_reservation_status: "expired" | "released";
  original_estimate_usd_micros: number;
  decision_id: string | null;
  message: string;
};

export type BudgetStatusResponse = {
  scope_type: string;
  scope_id: string;
  budget_state: string;
  legacy_financial_hold?: LegacyFinancialHold | null;
  legacy_provider_hold?: LegacyProviderHold | null;
  cap_usd_micros: number;
  reserved_usd_micros: number;
  actual_usd_micros: number;
  available_usd_micros: number;
  committed_usd_micros: number;
  burn_approx_usd_micros: number;
  burn_window_hours: number;
  burn_rate_usd_micros_per_hour: number;
  forecasted_24h_usd_micros: number;
  time_to_cap_seconds?: number | null;
  utilization_ratio: number;
  active_reservations_count: number;
  top_cost_drivers: Array<{
    skill_or_model_id: string;
    amount_usd_micros: number;
  }>;
  /** What subscription / local-model calls would have cost at API list price. Not money and
   *  in no cap (engine/boss_engine/services/included_spend.py). */
  included_in_subscription_usd_micros?: number;
  included_cost_drivers?: Array<{
    skill_or_model_id: string;
    amount_usd_micros: number;
  }>;
  /** Every cap measures its own period. The numbers above are the binding period's ("day" or
   *  "month" — the one nearest its cap); `periods` carries each. */
  period?: string | null;
  cap_name?: string | null;
  period_start?: string | null;
  resets_at?: string | null;
  /** "00:00 UTC", "00:00 UTC on 1 November". */
  resets_at_label?: string | null;
  budget_timezone?: string | null;
  periods?: Record<string, BudgetPeriodStatus>;
};

export type BudgetPeriodStatus = {
  period: string;
  cap_usd_micros: number;
  actual_usd_micros: number;
  reserved_usd_micros: number;
  committed_usd_micros: number;
  available_usd_micros: number;
  utilization_ratio: number;
  budget_state: string;
  period_start: string;
  resets_at: string;
  resets_at_label: string;
  cap_name: string;
};

export type BudgetReserveRequest = {
  project_id: string;
  category: string;
  amount_usd_micros: number;
  reason: string;
  job_id?: string;
  allocation_id?: string;
  expires_in_minutes?: number;
};

export type BudgetReserveResponse = {
  status: string;
  reservation_id?: string;
  decision_id?: string;
  budget_state: string;
  message: string;
};

export type BudgetAllocationSummary = {
  id: string;
  parent_allocation_id?: string;
  project_id: string;
  scope_type: string;
  scope_id: string;
  amount_usd_micros: number;
  child_allocated_usd_micros: number;
  reserved_usd_micros: number;
  actual_usd_micros: number;
  available_usd_micros: number;
  period_start: string;
  period_end: string;
  status: string;
  created_at: string;
};

export type BudgetAllocationListResponse = {
  project_id: string;
  items: BudgetAllocationSummary[];
};

export type BudgetAllocationCreateRequest = {
  project_id: string;
  scope_type: string;
  scope_id: string;
  amount_usd_micros: number;
  period_start: string;
  period_end: string;
  parent_allocation_id?: string;
  actor?: string;
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

export type BudgetConsumeRequest = {
  project_id: string;
  reservation_id: string;
  receipt_id: string;
  category: string;
  skill_or_model_id: string;
  estimated_usd_micros: number;
  actual_usd_micros?: number;
  actor: string;
};

export type BudgetConsumeResponse = {
  status: string;
  reservation_id: string;
  budget_state: string;
  message: string;
  cost_receipt: CostReceiptSummary;
};

export type BudgetForecastResponse = {
  scope_type: string;
  scope_id: string;
  horizon_hours: number;
  budget_state: string;
  cap_usd_micros: number;
  reserved_usd_micros: number;
  actual_usd_micros: number;
  available_usd_micros: number;
  committed_usd_micros: number;
  burn_rate_usd_micros_per_hour: number;
  forecasted_usd_micros: number;
  time_to_cap_seconds?: number | null;
  top_cost_drivers: Array<{
    skill_or_model_id: string;
    amount_usd_micros: number;
  }>;
};

export type DecisionCard = {
  id: string;
  task_id?: string;
  question: string;
  status: string;
  risk_tier: number;
  default_action?: string;
  reason?: string;
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
  limit?: number | null;
  offset: number;
};

export type DecisionActionRequest = {
  actor?: string;
  note?: string;
  delegate_to?: string;
  external_ref?: string;
  /** The `action_preview.content_hash` the approver saw; a moved-on preview is refused. */
  content_hash?: string;
  /** A person overriding BOSS's merge gate (e.g. merging with checks failing); needs a note. */
  force?: boolean;
};

export type DecisionDigestRequest = {
  project_id: string;
  channel?: string;
  actor?: string;
  max_items?: number;
  low_risk_max?: number;
  urgent_risk_min?: number;
  dedupe_key?: string;
  data_boundary?: DataBoundary;
};

export type DecisionDigestResponse = {
  status: string;
  artifact_id: string;
  outbox_message_id?: string;
  decision_count: number;
  urgent_count: number;
  batched_count: number;
  body: string;
};

export type OpenAIPlanRequest = {
  project_id: string;
  reservation_id: string;
  dedupe_key: string;
  prompt: string;
  system_prompt?: string;
  agent_module_id?: string;
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
  agent_module_id?: string;
  created_by_job_id?: string;
};

export type PullRequestProposalJobRequest = PullRequestProposalRequest & {
  task_id?: string;
  priority?: number;
  max_attempts?: number;
};

export type PullRequestProposalResponse = {
  status: string;
  data_boundary?: DataBoundary;
  artifact_id?: string;
  diff_artifact_id?: string;
  test_report_artifact_id?: string;
  security_review_artifact_id?: string;
  release_go_no_go_artifact_id?: string;
  receipt_id?: string;
  decision_id?: string;
  pull_request_url?: string;
  pull_request_number?: number;
  branch_name?: string;
  validation_status?: string;
  validation_summary?: string;
  validation_sources?: string[];
  local_validation_receipt_id?: string;
  message?: string;
};

export type CIConfigProposalRequest = {
  project_id: string;
  repo: string;
  language: string;
  framework?: string;
  target?: string;
  reservation_id: string;
  dedupe_key: string;
  branch_name: string;
  title?: string;
  task_id?: string;
  actor?: string;
  created_by_job_id?: string;
};

export type CIConfigProposalResponse = {
  status: string;
  pipeline_descriptor: Record<string, unknown>;
  generated_config?: Record<string, unknown>;
  proposal?: PullRequestProposalResponse;
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
  data_boundary?: DataBoundary;
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
  data_boundary?: DataBoundary;
  artifact_id?: string;
  receipt_id?: string;
  decision_id?: string;
  health_decision_id?: string;
  health_verification_artifact_id?: string;
  health_verification_status?: string;
  healthcheck_url?: string;
  rollout_id?: string;
  rollout_artifact_id?: string;
  rollout_status?: string;
  rollout_stage?: string;
  environment?: string;
  deployment_id?: number;
  deployment_status?: string;
  deployment_url?: string;
  merged_sha?: string;
  message?: string;
};

export type InfraProvisionReviewRequest = {
  project_id: string;
  task_id?: string;
  reservation_id: string;
  title: string;
  region: string;
  size: string;
  ttl_minutes: number;
  tags?: string[];
  data_boundary?: DataBoundary;
  actor?: string;
};

export type InfraProvisionReviewResponse = {
  status: string;
  artifact_id: string;
  decision_id: string;
  data_boundary: DataBoundary;
};

export type InfraProvisionRequest = {
  project_id: string;
  decision_id: string;
  reservation_id: string;
  dedupe_key: string;
  title: string;
  region: string;
  size: string;
  ttl_minutes: number;
  tags?: string[];
  task_id?: string;
  data_boundary?: DataBoundary;
  created_by_job_id?: string;
};

export type InfraProvisionJobRequest = InfraProvisionRequest & {
  actor?: string;
  priority?: number;
  max_attempts?: number;
};

export type InfraProvisionResponse = {
  status: string;
  resource_id?: string;
  artifact_id?: string;
  receipt_id?: string;
  decision_id?: string;
  provider_ref?: string;
  provider_resource_id?: string;
  resource_name?: string;
  expires_at?: string;
  data_boundary?: DataBoundary;
  message?: string;
};

export type InfraTerminationRequest = {
  project_id: string;
  dedupe_key: string;
  actor?: string;
  reason?: string;
  created_by_job_id?: string;
};

export type InfraTerminationJobRequest = InfraTerminationRequest & {
  priority?: number;
  max_attempts?: number;
};

export type InfraTerminationResponse = {
  status: string;
  resource_id?: string;
  artifact_id?: string;
  receipt_id?: string;
  decision_id?: string;
  provider_ref?: string;
  message?: string;
};

export type InfraJanitorRunRequest = {
  project_id: string;
  actor?: string;
  limit?: number;
};

export type InfraJanitorRunResponse = {
  status: string;
  project_id: string;
  queued_job_ids: string[];
  resource_ids: string[];
};

export type InfraResourceSummary = {
  id: string;
  project_id: string;
  task_id?: string;
  decision_id?: string;
  provider: string;
  resource_type: string;
  resource_name: string;
  external_resource_id: string;
  region: string;
  size: string;
  image: string;
  tags: string[];
  status: string;
  ttl_minutes: number;
  expires_at: string;
  terminated_at?: string;
  termination_reason?: string;
  provision_receipt_id?: string;
  provision_artifact_id?: string;
  termination_receipt_id?: string;
  termination_artifact_id?: string;
  data_boundary: DataBoundary;
  metadata: Record<string, unknown>;
  created_by_job_id?: string;
  updated_by_job_id?: string;
  created_at: string;
  updated_at: string;
};

export type InfraResourceListResponse = {
  project_id: string;
  items: InfraResourceSummary[];
};

export type DeploymentRolloutSummary = {
  id: string;
  project_id: string;
  task_id?: string;
  decision_id?: string;
  deployment_receipt_id?: string;
  deployment_artifact_id?: string;
  rollout_artifact_id?: string;
  title: string;
  environment: string;
  merged_sha: string;
  pull_request_url: string;
  branch_name?: string;
  status: string;
  current_stage: string;
  health_verification_status?: string;
  rollback_decision_id?: string;
  stages: Array<Record<string, unknown>>;
  created_by_job_id?: string;
  created_at: string;
  updated_at: string;
  deployment_run_id?: string;
  promotion_id?: string;
  rollback_reason?: string;
  health_observations: Array<Record<string, unknown>>;
  started_at?: string;
  finished_at?: string;
};

export type DeploymentRolloutListResponse = {
  project_id: string;
  items: DeploymentRolloutSummary[];
};

export type DeploymentRolloutStartRequest = {
  actor?: string;
  stages?: string[];
  thresholds?: Record<string, unknown>;
};

export type DeploymentRolloutAdvanceRequest = {
  actor?: string;
  observation: Record<string, unknown>;
};

export type DeploymentRolloutRollbackRequest = {
  actor?: string;
  reason: string;
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
  data_boundary: DataBoundary;
  created_at: string;
};

export type ArtifactDetail = ArtifactSummary & {
  content: Record<string, unknown>;
};

export type ArtifactListResponse = {
  project_id: string;
  items: ArtifactSummary[];
  limit: number;
  offset: number;
};

export type ChangeJournalItem = {
  id: string;
  project_id: string;
  task_id?: string;
  artifact_type: string;
  change_kind: string;
  title: string;
  summary?: string;
  diff_summary?: string;
  files_changed: string[];
  rollback_plan?: string;
  created_by_job_id?: string;
  data_boundary: DataBoundary;
  created_at: string;
};

export type ChangeJournalDetail = ChangeJournalItem & {
  content: Record<string, unknown>;
};

export type ChangeJournalResponse = {
  project_id: string;
  items: ChangeJournalItem[];
  limit: number;
  offset: number;
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
  data_boundary: DataBoundary;
  result?: Record<string, unknown>;
  error?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ReceiptListResponse = {
  project_id: string;
  items: ReceiptResponse[];
  limit: number;
  offset: number;
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

/** The counts an unscoped export reports, without the export. See GET /v1/audit/summary. */
export type AuditSummaryResponse = {
  project_id: string;
  generated_at: string;
  counts: AuditExportCounts;
};

export type AuditExportResponse = {
  project_id: string;
  project_name: string;
  workspace_name: string;
  org_name: string;
  generated_at: string;
  governance: Record<string, unknown>;
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

export type ComplianceEvidenceBundleRequest = {
  project_id: string;
  actor?: string;
  period_start?: string;
  period_end?: string;
};

export type ComplianceEvidenceCounts = {
  project_members: number;
  roles: number;
  policy_records: number;
  break_glass_grants: number;
  decisions: number;
  receipts: number;
  budget_reservations: number;
  cost_receipts: number;
  deadletters: number;
  compliance_events: number;
};

export type ComplianceEvidenceBundleResponse = {
  status: string;
  artifact_id: string;
  project_id: string;
  artifact_type: string;
  created_at: string;
  period: {
    period_start?: string | null;
    period_end?: string | null;
  };
  counts: ComplianceEvidenceCounts;
};

export type OutboxMessageCreateRequest = {
  project_id: string;
  channel: string;
  subject?: string;
  body: string;
  data_boundary?: DataBoundary;
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
  data_boundary: DataBoundary;
  dedupe_key: string;
  receipt_id?: string;
  created_at: string;
  updated_at: string;
};

export type OutboxMessageListResponse = {
  project_id: string;
  items: OutboxMessageSummary[];
  limit: number;
  offset: number;
};

export type CommsChannelPolicySummary = {
  id: string;
  project_id: string;
  provider: string;
  delivery_mode: string;
  allowed_data_boundaries: DataBoundary[];
  rollout_notes?: string;
  status: string;
  created_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type CommsChannelPolicyListResponse = {
  project_id: string;
  items: CommsChannelPolicySummary[];
};

export type CommsChannelPolicyUpsertRequest = {
  project_id: string;
  provider: string;
  delivery_mode: "disabled" | "draft_only" | "real_send_allowed";
  allowed_data_boundaries?: DataBoundary[];
  rollout_notes?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

/** POST /comms/outbox/{id}/queue: without `reservation_id` the API reserves the comms spend
 *  itself — a person never calls the system-only /v1/budget/reserve (identity int-b). */
export type OutboxQueueOnlyRequest = Omit<OutboxQueueRequest, "reservation_id"> & { reservation_id?: string };

export type OutboxQueueRequest = {
  project_id: string;
  reservation_id: string;
  dedupe_key: string;
  actor?: string;
  priority?: number;
  max_attempts?: number;
  urgency?: "low" | "normal" | "urgent";
  created_by_job_id?: string;
};

export type OutboxDryRunRequest = {
  project_id: string;
  actor?: string;
  dedupe_key?: string;
};

export type OutboxDeliveryResponse = {
  status: string;
  data_boundary?: DataBoundary;
  message_id?: string;
  receipt_id?: string;
  decision_id?: string;
  provider_ref?: string;
  message?: string;
};

export type OutboxDryRunResponse = {
  status: string;
  message_id?: string;
  receipt_id?: string;
  delivery_mode?: string;
  provider_send?: boolean;
  message?: string;
};

export type InboxMessageIngestRequest = {
  project_id: string;
  channel: string;
  subject?: string;
  body: string;
  raw_ref?: string;
  data_boundary?: DataBoundary;
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
  data_boundary: DataBoundary;
  parsed: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type InboxMessageListResponse = {
  project_id: string;
  items: InboxMessageSummary[];
  limit: number;
  offset: number;
};

export type RoleSummary = {
  id: string;
  project_id: string;
  role_key: string;
  display_name: string;
  autonomy_level: number;
  source_module_id?: string;
  source_module_version?: string;
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

export type RoleAutonomyCheckRequest = {
  project_id: string;
  actor?: string;
  action: string;
  risk_tier?: number;
  sandboxed?: boolean;
  policy_approved?: boolean;
};

export type RoleAutonomyCheckResponse = {
  project_id: string;
  role_key: string;
  allowed: boolean;
  reason: string;
  action: string;
  required_capability: string;
  risk_tier: number;
  sandboxed: boolean;
  policy_approved: boolean;
  current_level: number;
  required_level?: number | null;
  allowed_capabilities: string[];
  trust_score: number;
};

export type RoleScopeCheckRequest = {
  project_id: string;
  actor?: string;
  action: string;
  skill_id?: string;
  evidence_ref?: string;
};

export type RoleScopeCheckResponse = {
  project_id: string;
  role_key: string;
  allowed: boolean;
  reason: string;
  action: string;
  skill_id: string;
  allowed_skills: string[];
  forbidden_skills: string[];
  trust_score_before: number;
  trust_score_after: number;
  autonomy_level_before: number;
  autonomy_level_after: number;
  scope_violation_count: number;
  artifact_id?: string;
  autonomy_change_id?: string;
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

export type PackImportRequest = {
  project_id: string;
  yaml_content: string;
  actor?: string;
};

export type SkillImportRequest = {
  project_id: string;
  bundle: string;
  allow_untrusted?: boolean;
  enabled?: boolean;
  on_demand?: boolean | null;
  actor?: string;
};

export type SkillImportResponse = {
  id: string;
  project_id?: string;
  skill_id?: string;
  source?: string;
  enabled?: boolean;
  on_demand?: boolean;
  publisher?: Record<string, unknown>;
  provenance?: Record<string, unknown>;
};

export type SecretRefSummary = {
  id: string;
  project_id: string;
  scope_type: string;
  scope_id: string;
  ref_kind: string;
  ref_key: string;
  provider?: string;
  secret_kind: string;
  storage_backend: string;
  status: string;
  metadata: Record<string, unknown>;
  rotation_due_at?: string;
  last_verified_at?: string;
  supersedes_secret_ref_id?: string;
  overlap_expires_at?: string;
  revoked_at?: string;
  revoked_by?: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  credential_ref: string;
};

export type SecretRefListResponse = {
  project_id: string;
  items: SecretRefSummary[];
  limit: number;
  offset: number;
};

export type SecretRefCreateRequest = {
  project_id: string;
  ref_kind: string;
  ref_key: string;
  provider?: string;
  status?: string;
  metadata?: Record<string, unknown>;
  scope_type?: string;
  scope_id?: string;
  secret_kind?: string;
  storage_backend?: string;
  rotation_due_at?: string;
  actor?: string;
};

export type SecretRefUpdateRequest = {
  project_id: string;
  provider?: string;
  status?: string;
  metadata?: Record<string, unknown>;
  rotation_due_at?: string;
  actor?: string;
};

export type GitHubConnectionSummary = {
  id: string;
  project_id: string;
  connection_label: string;
  auth_method: "github_app" | "personal_access_token";
  credential_configured: boolean;
  app_id?: string;
  installation_id?: string;
  api_base_url: string;
  status: "configured" | "active" | "error" | "disabled" | "revoked";
  required_permissions: string[];
  granted_permissions: string[];
  missing_permissions: string[];
  account_login?: string;
  metadata: Record<string, unknown>;
  last_test_status?: string;
  last_tested_at?: string;
  last_error?: Record<string, unknown>;
  disabled_at?: string;
  disabled_by?: string;
  revoked_at?: string;
  revoked_by?: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type GitHubConnectionListResponse = {
  project_id: string;
  items: GitHubConnectionSummary[];
};

export type GitHubConnectionCreateRequest = {
  project_id: string;
  connection_label: string;
  auth_method: "github_app" | "personal_access_token";
  credential_ref: string;
  app_id?: string;
  installation_id?: string;
  api_base_url?: string;
  required_permissions?: string[];
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type GitHubEasyConnectRequest = {
  project_id: string;
  connection_label?: string;
  auth_method?: "github_app" | "personal_access_token";
  env_credential_key?: string;
  app_id?: string;
  installation_id?: string;
  api_base_url?: string;
  required_permissions?: string[];
  repository?: string;
  role?: "primary" | "source" | "upstream" | "mirror";
  test_after_link?: boolean;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type GitHubEasyConnectResponse = {
  connection: GitHubConnectionSummary;
  repository_binding?: GitHubRepositoryBindingSummary | null;
  test?: GitHubConnectionTestResponse | null;
  credential_ref: string;
};

export type GitHubAccessibleRepository = {
  full_name: string;
  owner: string;
  name: string;
  repository_id?: number | null;
  default_branch?: string | null;
  visibility?: string | null;
  archived?: boolean;
  permissions?: Record<string, boolean>;
  html_url?: string | null;
};

export type GitHubAccessibleRepositoryListResponse = {
  project_id: string;
  connection_id: string;
  items: GitHubAccessibleRepository[];
};

export type GitHubConnectionLifecycleRequest = {
  project_id: string;
  actor?: string;
};

export type GitHubRepositoryBindingSummary = {
  id: string;
  project_id: string;
  connection_id: string;
  repository_id?: number;
  owner: string;
  name: string;
  full_name: string;
  role: "primary" | "source" | "upstream" | "mirror";
  default_branch?: string;
  visibility?: string;
  archived: boolean;
  status: "active" | "disabled";
  permissions: Record<string, boolean>;
  capabilities: string[];
  metadata: Record<string, unknown>;
  last_synced_at?: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type GitHubRepositoryBindingListResponse = {
  project_id: string;
  items: GitHubRepositoryBindingSummary[];
};

export type GitHubRepositoryBindingCreateRequest = {
  project_id: string;
  connection_id: string;
  repository: string;
  role?: "primary" | "source" | "upstream" | "mirror";
  default_branch?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type GitHubRepositoryBindingUpdateRequest = {
  project_id: string;
  role?: "primary" | "source" | "upstream" | "mirror";
  status?: "active" | "disabled";
  default_branch?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type GitHubConnectionTestRequest = {
  project_id: string;
  repository_binding_id: string;
  dedupe_key: string;
  actor?: string;
};

export type GitHubConnectionTestResponse = {
  status: "passed" | "failed";
  duplicate: boolean;
  receipt_id: string;
  connection: GitHubConnectionSummary;
  repository_binding: GitHubRepositoryBindingSummary;
  detail: Record<string, unknown>;
};

export type GitHubBranchPolicySummary = {
  id: string;
  project_id: string;
  repository_binding_id: string;
  version: number;
  supersedes_policy_id: string | null;
  status: "active" | "superseded" | "disabled";
  protected_ref_patterns: string[];
  allowed_branch_prefixes: string[];
  branch_name_pattern: string;
  allowed_update_methods: string[];
  allowed_merge_methods: string[];
  required_checks: string[];
  required_approvals: number;
  require_pull_request: boolean;
  require_linear_history: boolean;
  require_signed_commits: boolean;
  automation_updates_enabled: boolean;
  force_push_allowed: boolean;
  metadata: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type GitHubBranchPolicyListResponse = {
  project_id: string;
  items: GitHubBranchPolicySummary[];
};

export type GitHubBranchPolicyCreateRequest = {
  project_id: string;
  repository_binding_id: string;
  protected_ref_patterns?: string[];
  allowed_branch_prefixes?: string[];
  branch_name_pattern?: string;
  allowed_update_methods?: string[];
  allowed_merge_methods?: string[];
  required_checks?: string[];
  required_approvals?: number;
  require_pull_request?: boolean;
  require_linear_history?: boolean;
  require_signed_commits?: boolean;
  automation_updates_enabled?: boolean;
  force_push_allowed?: boolean;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type GitHubBranchPolicyUpdateRequest = {
  project_id: string;
  status?: "active" | "disabled";
  protected_ref_patterns?: string[];
  allowed_branch_prefixes?: string[];
  branch_name_pattern?: string;
  allowed_update_methods?: string[];
  allowed_merge_methods?: string[];
  required_checks?: string[];
  required_approvals?: number;
  require_pull_request?: boolean;
  require_linear_history?: boolean;
  require_signed_commits?: boolean;
  automation_updates_enabled?: boolean;
  force_push_allowed?: boolean;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type GitHubBranchPolicyEvaluateRequest = {
  project_id: string;
  ref: string;
  operation: "observe" | "create" | "update" | "merge" | "delete";
  method?: string;
};

export type GitHubBranchPolicyEvaluationResponse = {
  policy_id: string;
  policy_version: number;
  status: "allowed" | "approval_required" | "blocked";
  ref: string;
  operation: string;
  method: string | null;
  protected: boolean;
  reasons: string[];
  required_checks: string[];
  required_approvals: number;
  require_pull_request: boolean;
  require_linear_history: boolean;
  require_signed_commits: boolean;
};

export type GitHubBranchSnapshotSummary = {
  id: string;
  project_id: string;
  repository_binding_id: string;
  branch_policy_id: string | null;
  branch_policy_version: number | null;
  source_ref: string;
  target_ref: string;
  source_commit_sha: string | null;
  source_tree_sha: string | null;
  target_commit_sha: string | null;
  target_tree_sha: string | null;
  merge_base_commit_sha: string | null;
  merge_base_tree_sha: string | null;
  ahead_by: number;
  behind_by: number;
  topology_classification:
    | "equal"
    | "tree_equivalent_rewrite"
    | "fast_forward"
    | "behind"
    | "diverged"
    | "missing";
  classification:
    | "equal"
    | "tree_equivalent_rewrite"
    | "fast_forward"
    | "behind"
    | "diverged"
    | "missing"
    | "protected_blocked";
  refs: Record<string, unknown>;
  comparison: Record<string, unknown>;
  protection: Record<string, unknown>;
  checks: Array<Record<string, unknown>>;
  policy_evaluation: Record<string, unknown>;
  receipt_id: string;
  dedupe_key: string;
  observed_by: string;
  observed_at: string;
  created_at: string;
};

export type GitHubBranchSnapshotListResponse = {
  project_id: string;
  items: GitHubBranchSnapshotSummary[];
};

export type GitHubBranchSnapshotCaptureRequest = {
  project_id: string;
  repository_binding_id: string;
  source_ref: string;
  target_ref: string;
  dedupe_key: string;
  actor?: string;
};

export type GitHubBranchSnapshotCaptureResponse = {
  status: "succeeded" | "failed";
  duplicate: boolean;
  receipt_id: string;
  snapshot: GitHubBranchSnapshotSummary | null;
  error: Record<string, unknown> | null;
};

export type GitHubBranchLeaseSummary = {
  id: string;
  project_id: string;
  repository_binding_id: string;
  ref_name: string;
  owner_kind: "task" | "workflow_run" | "code_task_run" | "agent" | "manual";
  owner_id: string;
  run_id: string | null;
  worktree_path: string | null;
  acquire_key: string;
  fencing_token: number;
  status: "active" | "released" | "expired" | "reclaimed";
  acquired_by: string;
  acquired_at: string;
  heartbeat_at: string;
  lease_expires_at: string;
  released_by: string | null;
  released_at: string | null;
  release_reason: string | null;
  reclaimed_from_lease_id: string | null;
  created_at: string;
  updated_at: string;
};

export type GitHubBranchLeaseListResponse = {
  project_id: string;
  items: GitHubBranchLeaseSummary[];
};

export type GitHubBranchLeaseAcquireRequest = {
  project_id: string;
  repository_binding_id: string;
  ref_name: string;
  owner_kind: "task" | "workflow_run" | "code_task_run" | "agent" | "manual";
  owner_id: string;
  run_id?: string;
  worktree_path?: string;
  acquire_key: string;
  ttl_seconds?: number;
  actor?: string;
};

export type GitHubBranchLeaseAcquireResponse = {
  lease: GitHubBranchLeaseSummary;
  lease_token: string | null;
  duplicate: boolean;
};

export type GitHubBranchLeaseHeartbeatRequest = {
  project_id: string;
  lease_token: string;
  fencing_token: number;
  ttl_seconds?: number;
  actor?: string;
};

export type GitHubBranchLeaseReleaseRequest = {
  project_id: string;
  lease_token: string;
  fencing_token: number;
  reason: string;
  actor?: string;
};

export type GitHubBranchLeaseReclaimRequest = {
  project_id: string;
  reason: string;
  actor?: string;
};

export type GitHubCodeSyncRunSummary = {
  id: string;
  project_id: string;
  repository_binding_id: string;
  branch_policy_id: string;
  branch_policy_version: number;
  initial_snapshot_id: string;
  result_snapshot_id: string | null;
  branch_lease_id: string | null;
  fencing_token: number | null;
  source_ref: string;
  target_ref: string;
  topology_classification:
    | "equal"
    | "tree_equivalent_rewrite"
    | "fast_forward"
    | "behind"
    | "diverged"
    | "missing";
  action:
    | "noop"
    | "preserve_remote_identity"
    | "fast_forward_target"
    | "fast_forward_source"
    | "create_resolution_branch"
    | "blocked";
  status:
    | "planned"
    | "pending_approval"
    | "approved"
    | "applying"
    | "succeeded"
    | "noop"
    | "conflicted"
    | "blocked"
    | "failed"
    | "deadlettered";
  dedupe_key: string;
  changeset_id: string | null;
  conflict_set_id: string | null;
  decision_id: string | null;
  resolution_branch: string | null;
  rollback_ref: string | null;
  rollback_sha: string | null;
  apply_attempts: number;
  receipt_ids: string[];
  resource_receipt_ids: string[];
  error: Record<string, unknown> | null;
  created_by: string;
  approved_by: string | null;
  applied_by: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

export type GitHubCodeSyncRunListResponse = {
  project_id: string;
  items: GitHubCodeSyncRunSummary[];
};

export type GitHubCodeSyncPlanRequest = {
  project_id: string;
  repository_binding_id: string;
  snapshot_id: string;
  dedupe_key: string;
  resolution_branch?: string;
  actor?: string;
};

export type GitHubCodeSyncApplyRequest = {
  project_id: string;
  lease_id: string;
  lease_token: string;
  fencing_token: number;
  actor?: string;
};

export type GitHubConflictSetSummary = {
  id: string;
  project_id: string;
  code_sync_run_id: string;
  repository_binding_id: string;
  resolution_branch: string;
  resolution_branch_head_sha: string;
  base_commit_sha: string | null;
  base_tree_sha: string | null;
  ours_commit_sha: string;
  ours_tree_sha: string;
  theirs_commit_sha: string;
  theirs_tree_sha: string;
  status: "open" | "resolution_proposed" | "approved" | "resolved" | "blocked";
  files: Array<Record<string, unknown>>;
  conflict_count: number;
  decision_id: string | null;
  resolved_by_plan_id: string | null;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
};

export type GitHubConflictSetListResponse = {
  project_id: string;
  items: GitHubConflictSetSummary[];
};

export type GitHubResolutionPlanSummary = {
  id: string;
  project_id: string;
  conflict_set_id: string;
  version: number;
  status: "proposed" | "approved" | "applied" | "rejected" | "failed";
  resolved_files: Array<Record<string, unknown>>;
  validation_evidence: Record<string, unknown>;
  independent_review: Record<string, unknown>;
  decision_id: string | null;
  changeset_id: string | null;
  result_commit_sha: string | null;
  created_by: string;
  approved_by: string | null;
  applied_by: string | null;
  created_at: string;
  updated_at: string;
  applied_at: string | null;
};

export type GitHubResolutionPlanListResponse = {
  project_id: string;
  items: GitHubResolutionPlanSummary[];
};

export type GitHubResolutionPlanCreateRequest = {
  project_id: string;
  resolved_files: Array<{
    path: string;
    content: string;
    mode?: "100644" | "100755";
  }>;
  validation_evidence: {
    red: {
      command: string;
      exit_code: number;
      evidence_ref: string;
      [key: string]: unknown;
    };
    green: {
      command: string;
      exit_code: number;
      evidence_ref: string;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  actor?: string;
};

export type GitHubResolutionPlanApproveRequest = {
  project_id: string;
  review_evidence: {
    verdict: "approved";
    summary: string;
    [key: string]: unknown;
  };
  actor: string;
};

export type GitHubResolutionPlanApplyRequest = {
  project_id: string;
  lease_id: string;
  lease_token: string;
  fencing_token: number;
  actor?: string;
};

export type GitHubResolutionApplyResponse = {
  plan: GitHubResolutionPlanSummary;
  conflict_set: GitHubConflictSetSummary;
  sync_run: GitHubCodeSyncRunSummary;
};

export type AgentModuleSummary = {
  module_id: string;
  version: string;
  display_name: string;
  description: string;
  publisher: string;
  category: string;
  status: string;
  role_key?: string;
  job_kinds: string[];
  default_skill_ids: string[];
  tags: string[];
  price_model?: string;
  price_usd_micros?: number;
  required_entitlements: string[];
  entitlement_status: string;
  config_schema: Record<string, unknown>;
  installed: boolean;
  installed_by?: string;
  installed_at?: string;
  updated_at?: string;
  installed_version?: string;
  config: Record<string, unknown>;
};

export type AgentModuleDetail = AgentModuleSummary & {
  prompt_bundle: Record<string, unknown>;
  runtime: Record<string, unknown>;
  marketplace: Record<string, unknown>;
  role: Record<string, unknown>;
  role_result: Record<string, unknown>;
  entitlement_result: Record<string, unknown>;
  install_status?: string;
};

export type AgentModuleListResponse = {
  project_id: string;
  items: AgentModuleSummary[];
};

export type AgentModuleInstallRequest = {
  project_id: string;
  module_id: string;
  actor?: string;
  config?: Record<string, unknown>;
};

export type AgentModuleLifecycleRequest = {
  project_id: string;
  actor?: string;
  reason?: string;
};













export type AgentModuleInstallRecordSummary = {
  id: string;
  project_id: string;
  module_id: string;
  action: string;
  from_version?: string;
  to_version?: string;
  actor: string;
  status: string;
  entitlement_result: Record<string, unknown>;
  rollback_snapshot: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type AgentModuleInstallRecordListResponse = {
  project_id: string;
  items: AgentModuleInstallRecordSummary[];
};

export type AgentRuntimeResolveRequest = {
  project_id: string;
  job_kind: string;
  agent_module_id?: string;
  task_payload?: Record<string, unknown>;
  actor?: string;
};

export type AgentRuntimeResolveResponse = {
  project_id: string;
  job_kind: string;
  matched: boolean;
  agent_module?: Record<string, unknown> | null;
  compiled_prompt: Record<string, unknown>;
  runtime: Record<string, unknown>;
  role: Record<string, unknown>;
  config: Record<string, unknown>;
};

export type SkillSummary = {
  id: string;
  kind: string;
  system: boolean;
  load: "eager" | "on_demand";
  risk_tier: number;
  description: string;
  mock_mode: boolean;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  cost: Record<string, unknown>;
  retry: Record<string, unknown>;
  permissions: Record<string, unknown>;
  data_boundary: string;
  policy: Record<string, unknown>;
};

export type SkillListResponse = {
  items: SkillSummary[];
};

export type ModelAdapterSummary = {
  id: string;
  provider: string;
  source_id: string;
  source_kind: string;
  access_class: string;
  entitlement_kind: string;
  auth_method: string;
  connection_ref?: string;
  supported_models: string[];
  task_strengths: string[];
  allowed_workload_classes: string[];
  multi_tenant_allowed: boolean;
  runtime_supported: boolean;
  credential_present: boolean;
  official_connection_required: boolean;
  policy_notes: string[];
};

export type ModelAdapterListResponse = {
  items: ModelAdapterSummary[];
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
  credential_secret_ref_id?: string;
  cost_mode: string;
  limit_shape: Record<string, unknown>;
  supported_models: string[];
  task_strengths: string[];
  risk_tier_max: number;
  status: string;
  effective_status: string;
  auth_method: string;
  connection_status: string;
  connection_ref?: string;
  oauth_scopes: string[];
  connection_metadata: Record<string, unknown>;
  policy_notes: string[];
  observed_capacity: Record<string, unknown>;
  runtime_supported: boolean;
  created_at: string;
  updated_at: string;
  /** The SAME pill the Models row shows for this source, decided from its last real proof
   *  (BOSSAPP-769). null when the source has no Models row; absent on an older API. */
  pill?: EntitlementSourcePill | null;
};

/** A source's proven state, as the Models row shows it (GET /v1/entitlements/sources `pill`). */
export type EntitlementSourcePill = {
  state: string;
  label: string;
  tone: "green" | "amber" | "red" | "grey" | "busy";
  checked_at: string | null;
};

// ── SUBSCRIPTION SIGN-IN (BOSSAPP-769) ──────────────────────────────────────────────────────
// The provider's own login runs inside the BOSS server; nothing is pasted, dropped or imported.
// POST /v1/setup/sign-in, GET|DELETE /v1/setup/sign-in/{id}, POST /v1/setup/sign-in/{id}/code.

export type SignInProvider = "codex" | "claude_code" | "grok";

export type SignInState = "starting" | "waiting" | "signed_in" | "failed" | "expired" | "cancelled";

export type SignInSession = {
  sign_in_id: string;
  provider: SignInProvider;
  state: SignInState;
  /** The URL the provider's CLI printed (open in a browser). */
  verification_url: string | null;
  /** The one-time code it printed (not a credential). */
  user_code: string | null;
  /** True when the CLI waits for a code pasted back. */
  needs_code_input: boolean;
  /** ISO. */
  expires_at: string | null;
  /** One plain sentence each, always set. */
  headline: string;
  fix: string;
};

export type SignInStartRequest = { project_id: string; actor?: string; provider: SignInProvider; fresh?: boolean };
export type SignInCodeRequest = { project_id: string; code: string };

/** GET /v1/setup `detected[].found`: a subscription is signed in, needs a sign-in, or its CLI is
 *  not on the server; a local model is found or not. */
export type SetupDetectedFound = "signed_in" | "needs_sign_in" | "not_on_server" | "found";

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
  auth_method?: string;
  connection_status?: string;
  connection_ref?: string;
  oauth_scopes?: string[];
  connection_metadata?: Record<string, unknown>;
  policy_notes?: string[];
  observed_capacity?: Record<string, unknown>;
  runtime_supported?: boolean;
  actor?: string;
};

export type EntitlementSourceConnectRequest = {
  project_id: string;
  connection_ref: string;
  auth_method?: string;
  oauth_scopes?: string[];
  connection_metadata?: Record<string, unknown>;
  observed_capacity?: Record<string, unknown>;
  actor?: string;
};

export type EntitlementSourceDisconnectRequest = {
  project_id: string;
  reason?: string;
  actor?: string;
};

export type ModelCredentialUploadRequest = {
  project_id: string;
  value: string;
  backend?: "platform_secret_store" | "vault" | "local_runner";
  actor?: string;
};

export type ModelCredentialWriteSummary = {
  source: EntitlementSourceSummary;
  secret_ref_id: string;
  backend: string;
  fingerprint?: string;
  last4?: string;
  raw_secret_stored: false;
};

export type EntitlementConnectionSessionSummary = {
  id: string;
  project_id: string;
  source_id: string;
  provider: string;
  auth_method: string;
  connection_mode: string;
  status: string;
  requested_scopes: string[];
  authorization_url?: string;
  device_code?: string;
  user_code?: string;
  verification_uri?: string;
  login_command?: string;
  connection_ref?: string;
  instructions: Record<string, unknown>;
  metadata: Record<string, unknown>;
  error: Record<string, unknown>;
  requested_by: string;
  completed_by?: string;
  expires_at?: string;
  created_at: string;
  updated_at: string;
  completed_at?: string;
};

export type EntitlementConnectionSessionListResponse = {
  project_id: string;
  items: EntitlementConnectionSessionSummary[];
};

export type EntitlementConnectionSessionCreateRequest = {
  project_id: string;
  connection_mode?: string;
  requested_scopes?: string[];
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EntitlementConnectionSessionCompleteRequest = {
  project_id: string;
  connection_ref: string;
  oauth_scopes?: string[];
  connection_metadata?: Record<string, unknown>;
  observed_capacity?: Record<string, unknown>;
  actor?: string;
};

export type EntitlementConnectionSessionCompleteResponse = {
  project_id: string;
  session: EntitlementConnectionSessionSummary;
  source: EntitlementSourceSummary;
};

export type EntitlementPoolSummary = {
  id: string;
  project_id: string;
  provider: string;
  source_id: string;
  source_kind: string;
  access_class: string;
  auth_method: string;
  connection_status: string;
  effective_status: string;
  owner_type: string;
  owner_id: string;
  allowed_workload_classes: string[];
  routing_modes: string[];
  capacity_state: string;
  cooldown_until?: string;
  multi_tenant_allowed: boolean;
  budget_policy_ref: string;
  runtime_supported: boolean;
  supported_models: string[];
  task_strengths: string[];
  risk_tier_max: number;
  observed_capacity: Record<string, unknown>;
  policy_notes: string[];
  updated_at?: string;
};

export type EntitlementPoolListResponse = {
  project_id: string;
  items: EntitlementPoolSummary[];
};

export type CapacityReceiptSummary = {
  id: string;
  project_id: string;
  entitlement_pool_id: string;
  source_id: string;
  provider: string;
  capacity_kind: string;
  model_route_decision_id?: string;
  provider_ref?: string;
  usage_units: Record<string, unknown>;
  capacity_state_after: string;
  dedupe_key: string;
  status: string;
  observed_capacity: Record<string, unknown>;
  created_by: string;
  created_at: string;
};

export type EntitlementPoolProbeRequest = {
  project_id: string;
  observed_capacity: Record<string, unknown>;
  usage_units?: Record<string, unknown>;
  provider_ref?: string;
  model_route_decision_id?: string;
  dedupe_key?: string;
  status?: string;
  actor?: string;
};

export type EntitlementPoolProbeResponse = {
  project_id: string;
  pool: EntitlementPoolSummary;
  receipt: CapacityReceiptSummary;
  duplicate: boolean;
};

export type RoutingPreviewRequest = {
  project_id: string;
  skill_id: string;
  task_kind: string;
  workload_class: string;
  risk_tier?: number;
  data_boundary?: DataBoundary;
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
  data_boundary: DataBoundary;
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
  data_boundary: DataBoundary;
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

export type ModelRouteDecisionSummary = {
  id: string;
  project_id: string;
  task_id?: string;
  skill_id?: string;
  task_kind: string;
  workload_class: string;
  risk_tier: number;
  selected_entitlement_pool_id?: string;
  selected_source_id?: string;
  selected_provider?: string;
  selected_model_id?: string;
  routing_mode: string;
  rejected_sources: Array<Record<string, unknown>>;
  budget_state: string;
  data_boundary: DataBoundary;
  capacity_snapshot: Record<string, unknown>;
  policy_result: Record<string, unknown>;
  created_at: string;
};

export type ModelRouteDecisionListResponse = {
  project_id: string;
  items: ModelRouteDecisionSummary[];
};

export type ModelRouteDecisionListFilters = {
  limit?: number;
  skill_id?: string;
  task_kind?: string;
  workload_class?: string;
  provider?: string;
  source_id?: string;
  model_id?: string;
  routing_mode?: string;
  budget_state?: string;
  data_boundary?: DataBoundary;
  risk_tier?: number;
  denial_reason?: string;
  denied_only?: boolean;
  selected_only?: boolean;
};

export type ModelRouteDecisionSimulateRequest = RoutingPreviewRequest;

export type ModelRouteDecisionSimulateResponse = {
  project_id: string;
  decision: ModelRouteDecisionSummary;
};

export type GitHubMergeAuthorizationSummary = {
  id: string;
  project_id: string;
  repository_binding_id: string;
  branch_policy_id: string;
  branch_policy_version: number;
  code_sync_run_id: string | null;
  resolution_plan_id: string | null;
  pull_request_number: number;
  pull_request_url: string;
  source_ref: string;
  target_ref: string;
  head_commit_sha: string;
  head_tree_sha: string;
  base_commit_sha: string;
  base_tree_sha: string;
  merge_method: "merge" | "squash" | "rebase";
  status:
    | "pending_approval"
    | "authorized"
    | "queued"
    | "applying"
    | "merged"
    | "blocked"
    | "expired"
    | "failed"
    | "deadlettered"
    | "cancelled";
  dedupe_key: string;
  required_checks: string[];
  observed_checks: Array<Record<string, unknown>>;
  required_approvals: number;
  observed_reviews: Array<Record<string, unknown>>;
  evidence_bundle: Record<string, unknown>;
  policy_evaluation: Record<string, unknown>;
  authorized_by: string | null;
  authorized_at: string | null;
  expires_at: string;
  queue_position: number;
  queued_at: string | null;
  branch_lease_id: string | null;
  fencing_token: number | null;
  changeset_id: string;
  apply_attempts: number;
  receipt_ids: string[];
  resource_receipt_ids: string[];
  result_merge_sha: string | null;
  result_target_tree_sha: string | null;
  rollback_plan: Record<string, unknown>;
  error: Record<string, unknown> | null;
  created_by: string;
  applied_by: string | null;
  created_at: string;
  updated_at: string;
  merged_at: string | null;
};

export type GitHubMergeAuthorizationListResponse = {
  project_id: string;
  items: GitHubMergeAuthorizationSummary[];
};

export type GitHubMergeAuthorizationCreateRequest = {
  project_id: string;
  repository_binding_id: string;
  pull_request_number: number;
  source_ref: string;
  target_ref: string;
  merge_method?: "merge" | "squash" | "rebase";
  dedupe_key: string;
  evidence_bundle: Record<string, unknown>;
  expires_in_seconds?: number;
  code_sync_run_id?: string;
  resolution_plan_id?: string;
  actor?: string;
};

export type GitHubMergeAuthorizationApproveRequest = {
  project_id: string;
  review_evidence: Record<string, unknown>;
  actor: string;
};

export type GitHubMergeAuthorizationEnqueueRequest = {
  project_id: string;
  actor?: string;
};

export type GitHubMergeAuthorizationApplyRequest = {
  project_id: string;
  lease_id: string;
  lease_token: string;
  fencing_token: number;
  actor?: string;
};

export type GitHubReconciliationRunSummary = {
  id: string;
  project_id: string;
  repository_binding_id: string;
  trigger_signal_id: string | null;
  repair_of_run_id: string | null;
  source: "webhook" | "poll" | "repair";
  status:
    | "queued"
    | "running"
    | "succeeded"
    | "failed"
    | "deadlettered"
    | "cancelled";
  dedupe_key: string;
  requested_refs: string[];
  attempt_count: number;
  max_attempts: number;
  job_id: string | null;
  observation_ids: string[];
  receipt_ids: string[];
  rate_limit_status: Record<string, unknown>;
  next_attempt_at: string | null;
  deadletter_id: string | null;
  error: Record<string, unknown> | null;
  created_by: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type GitHubReconciliationRunListResponse = {
  project_id: string;
  items: GitHubReconciliationRunSummary[];
};

export type GitHubReconciliationSignalSummary = {
  id: string;
  project_id: string;
  repository_binding_id: string;
  inbound_webhook_id: string | null;
  source: "webhook" | "poll" | "repair";
  delivery_id: string | null;
  provider_event: string | null;
  action: string | null;
  ref_name: string;
  expected_commit_sha: string | null;
  expected_deleted: boolean;
  payload_hash: string | null;
  dedupe_key: string;
  status:
    | "pending"
    | "processing"
    | "applied"
    | "stale"
    | "failed"
    | "deadlettered";
  reconciliation_run_id: string | null;
  observed_commit_sha: string | null;
  error: Record<string, unknown> | null;
  provider_occurred_at: string | null;
  processed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type GitHubReconciliationSignalListResponse = {
  project_id: string;
  items: GitHubReconciliationSignalSummary[];
};

export type GitHubRepositoryRefStateSummary = {
  id: string;
  project_id: string;
  repository_binding_id: string;
  ref_name: string;
  exists: boolean;
  commit_sha: string | null;
  tree_sha: string | null;
  protected: boolean;
  protection: Record<string, unknown>;
  latest_observation_id: string;
  latest_signal_id: string | null;
  latest_receipt_id: string;
  observed_at: string;
  created_at: string;
  updated_at: string;
};

export type GitHubRepositoryRefStateListResponse = {
  project_id: string;
  items: GitHubRepositoryRefStateSummary[];
};

export type GitHubReconciliationPollRequest = {
  project_id: string;
  repository_binding_id?: string;
  refs?: string[];
  dedupe_key: string;
  actor?: string;
};

export type GitHubReconciliationPollResponse = {
  project_id: string;
  items: GitHubReconciliationRunSummary[];
};

export type GitHubReconciliationExecuteRequest = {
  project_id: string;
  job_id: string;
  worker_id: string;
  lease_token: string;
};

export type GitHubReconciliationExecuteResponse = {
  run: GitHubReconciliationRunSummary;
  job: Record<string, unknown>;
};

export type GitHubReconciliationRepairRequest = {
  project_id: string;
  dedupe_key: string;
  actor?: string;
};

export type ProviderAccountSummary = {
  id: string;
  project_id: string;
  provider: string;
  account_label: string;
  auth_method: string;
  credential_ref?: string;
  credential_refs: Record<string, string>;
  connection_metadata: Record<string, unknown>;
  scopes: string[];
  status: string;
  owner_actor?: string;
  credential_version: number;
  credential_rotated_at?: string;
  disabled_at?: string;
  disabled_by?: string;
  revoked_at?: string;
  revoked_by?: string;
  read_only: boolean;
  deploy_capable: boolean;
  billing_readable: boolean;
  destructive_actions_disabled: boolean;
  capabilities: string[];
  missing_permissions: string[];
  last_test_status?: string;
  last_tested_at?: string;
  last_error?: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type ProviderAccountListResponse = {
  project_id: string;
  items: ProviderAccountSummary[];
};

export type ProviderAccountCreateRequest = {
  project_id: string;
  provider: string;
  account_label: string;
  auth_method?: string;
  credential_ref?: string;
  credential_refs?: Record<string, string>;
  connection_metadata?: Record<string, unknown>;
  scopes?: string[];
  read_only?: boolean;
  deploy_capable?: boolean;
  billing_readable?: boolean;
  destructive_actions_disabled?: boolean;
  actor?: string;
};

export type ProviderAccountUpdateRequest = {
  project_id: string;
  account_label?: string;
  scopes?: string[];
  read_only?: boolean;
  deploy_capable?: boolean;
  billing_readable?: boolean;
  destructive_actions_disabled?: boolean;
  connection_metadata?: Record<string, unknown>;
  owner_actor?: string;
  actor?: string;
};

export type ProviderAccountLifecycleRequest = {
  project_id: string;
  actor?: string;
};

export type ProviderCredentialRotateRequest = {
  project_id: string;
  credential_ref?: string;
  credential_refs?: Record<string, string>;
  overlap_minutes?: number;
  actor?: string;
};

export type ProviderCredentialRotationResponse = {
  provider_account: ProviderAccountSummary;
  prior_secret_refs: SecretRefSummary[];
  active_secret_refs: SecretRefSummary[];
  overlap_expires_at: string;
};

export type ProviderCredentialSlotSummary = {
  key: string;
  secret_kind: string;
  required: boolean;
  default_ref?: string;
};

export type ProviderAuthMethodSummary = {
  key: string;
  runtime_status: string;
  credential_slots: ProviderCredentialSlotSummary[];
};

export type ProviderDefinitionSummary = {
  provider: string;
  display_name: string;
  category: string;
  adapter_status: string;
  default_auth_method: string;
  auth_methods: ProviderAuthMethodSummary[];
  capabilities: string[];
};

export type ProviderDefinitionListResponse = {
  items: ProviderDefinitionSummary[];
};

export type ProviderHealthSummary = {
  id: string;
  project_id: string;
  provider_account_id?: string;
  provider: string;
  status: string;
  api_available: boolean;
  rate_limit_status: string;
  usage_cap_status: string;
  last_checked_at: string;
  detail: Record<string, unknown>;
  created_at: string;
};

export type ProviderHealthListResponse = {
  project_id: string;
  items: ProviderHealthSummary[];
};

export type ProviderConnectionTestResponse = {
  status: string;
  provider_account: ProviderAccountSummary;
  provider_health: ProviderHealthSummary;
  detail: Record<string, unknown>;
};

export type ResourceSyncRequest = {
  project_id: string;
  provider_account_id?: string;
  actor?: string;
};

export type ResourceSyncRunSummary = {
  id: string;
  status: string;
  provider?: string;
  provider_account_id?: string;
  resources_seen: number;
  resources_changed: number;
  costs_seen: number;
  billing_status?: string;
  billing_message?: string;
  provider_health_id?: string;
  drift_report_id?: string;
  deadletter_id?: string;
  inventory_snapshot_artifact_id?: string;
  error?: Record<string, unknown>;
};

export type ResourceSyncResponse = {
  project_id: string;
  runs: ResourceSyncRunSummary[];
  resources_seen: number;
  resources_changed: number;
  costs_seen: number;
};

export type ExternalResourceSummary = {
  id: string;
  project_id: string;
  provider_account_id?: string;
  provider: string;
  resource_type: string;
  external_id: string;
  name: string;
  state: string;
  ownership: string;
  environment: string;
  owner_role?: string;
  task_id?: string;
  deployment_target_id?: string;
  receipt_id?: string;
  budget_allocation_id?: string;
  health_status: string;
  estimated_monthly_usd_micros?: number;
  actual_monthly_usd_micros?: number;
  cost_confidence: string;
  last_seen_at: string;
  tags: string[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ExternalResourceListResponse = {
  project_id: string;
  items: ExternalResourceSummary[];
  limit: number;
  offset: number;
};

export type CostEventSummary = {
  id: string;
  project_id: string;
  provider_account_id?: string;
  external_resource_id?: string;
  billing_account_id?: string;
  task_id?: string;
  role_key?: string;
  provider: string;
  category: string;
  amount_usd_micros: number;
  cost_layer: string;
  confidence: string;
  source_ref?: string;
  period_start?: string;
  period_end?: string;
  receipt_id?: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type CostEventListResponse = {
  project_id: string;
  items: CostEventSummary[];
  limit: number;
  offset: number;
};

export type CostAnomalySummary = {
  id: string;
  project_id: string;
  provider_account_id?: string;
  cost_event_id?: string;
  decision_id?: string;
  provider: string;
  anomaly_type: string;
  severity: string;
  status: string;
  amount_usd_micros: number;
  threshold_usd_micros: number;
  summary: string;
  recommended_actions: Array<Record<string, unknown>>;
  created_at: string;
  resolved_at?: string;
};

export type CostAnomalyListResponse = {
  project_id: string;
  items: CostAnomalySummary[];
  limit: number;
  offset: number;
};

export type DriftReportSummary = {
  id: string;
  project_id: string;
  provider_account_id?: string;
  classification: string;
  summary: string;
  unmanaged_count: number;
  orphaned_count: number;
  security_impacting_count: number;
  production_impacting_count: number;
  recommendations: Array<Record<string, unknown>>;
  created_at: string;
};

export type DriftReportListResponse = {
  project_id: string;
  items: DriftReportSummary[];
};

export type ReconciliationRunSummary = {
  id: string;
  project_id: string;
  provider_account_id?: string;
  provider?: string;
  status: string;
  resources_seen: number;
  resources_changed: number;
  costs_seen: number;
  started_by: string;
  started_at: string;
  finished_at?: string;
  error?: Record<string, unknown>;
  metadata: Record<string, unknown>;
};

export type ReconciliationRunListResponse = {
  project_id: string;
  items: ReconciliationRunSummary[];
};

export type DeploymentTargetSummary = {
  id: string;
  project_id: string;
  app_name: string;
  repo: string;
  environment: string;
  provider_account_id?: string;
  target_kind: string;
  target_ref: string;
  domains: string[];
  required_resources: Array<Record<string, unknown>>;
  secret_refs: Array<string | Record<string, unknown>>;
  health_checks: Array<Record<string, unknown>>;
  rollback_points: Array<Record<string, unknown>>;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type DeploymentTargetListResponse = {
  project_id: string;
  items: DeploymentTargetSummary[];
  limit: number;
  offset: number;
};

export type DeploymentTargetCreateRequest = {
  project_id: string;
  app_name: string;
  repo: string;
  environment: string;
  provider_account_id?: string;
  target_kind: string;
  target_ref: string;
  domains?: string[];
  required_resources?: Array<Record<string, unknown>>;
  secret_refs?: Array<string | Record<string, unknown>>;
  health_checks?: Array<Record<string, unknown>>;
  rollback_points?: Array<Record<string, unknown>>;
  actor?: string;
};

export type DeploymentEnvironmentPolicySummary = {
  id: string;
  project_id: string;
  environment: string;
  runtime_mode: string;
  allowed_target_kinds: string[];
  requires_change_set: boolean;
  requires_budget_reservation: boolean;
  requires_approval: boolean;
  requires_health_check: boolean;
  requires_rollback_plan: boolean;
  rollout_notes?: string;
  status: string;
  created_by: string;
  metadata: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type DeploymentEnvironmentPolicyListResponse = {
  project_id: string;
  items: DeploymentEnvironmentPolicySummary[];
  limit: number;
  offset: number;
};

export type DeploymentEnvironmentPolicyUpsertRequest = {
  project_id: string;
  environment: string;
  runtime_mode?: string;
  allowed_target_kinds?: string[];
  requires_change_set?: boolean;
  requires_budget_reservation?: boolean;
  requires_approval?: boolean;
  requires_health_check?: boolean;
  requires_rollback_plan?: boolean;
  rollout_notes?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type DeploymentRunSummary = {
  id: string;
  project_id: string;
  deployment_target_id: string;
  changeset_id?: string;
  decision_id?: string;
  release_artifact_id?: string;
  health_artifact_id?: string;
  rollback_artifact_id?: string;
  app_name: string;
  environment: string;
  repo: string;
  target_kind: string;
  target_ref: string;
  commit_ref?: string;
  status: string;
  release_packet: Record<string, unknown>;
  health_check_results: Array<Record<string, unknown>>;
  rollback_plan: string[];
  release_receipt_ids: Array<Record<string, unknown>>;
  created_by: string;
  created_at: string;
  updated_at: string;
  started_at?: string;
  finished_at?: string;
  failure_reason?: string;
};

export type DeploymentRunListResponse = {
  project_id: string;
  items: DeploymentRunSummary[];
};

export type DeploymentRunCreateRequest = {
  project_id: string;
  deployment_target_id: string;
  commit_ref?: string;
  release_packet?: Record<string, unknown>;
  expected_cost_usd_micros?: number;
  reservation_id?: string;
  actor?: string;
};

export type DeploymentRunHealthCheckRequest = {
  actor?: string;
};

export type DeploymentRunHealthCheckResponse = {
  deployment_run: DeploymentRunSummary;
  health_artifact_id: string;
  rollback_artifact_id?: string;
  decision_id?: string;
};

export type DeploymentPromotionStartRequest = {
  project_id: string;
  staging_target_id: string;
  production_target_id: string;
  commit_ref: string;
  staging_reservation_id?: string;
  production_reservation_id?: string;
  staging_expected_cost_usd_micros?: number;
  production_expected_cost_usd_micros?: number;
  release_packet?: Record<string, unknown>;
  actor?: string;
};

export type DeploymentPromotionAdvanceRequest = {
  actor?: string;
};

export type DeploymentPromotionSummary = {
  id: string;
  project_id: string;
  app_name: string;
  repo: string;
  commit_ref: string;
  staging_target_id: string;
  production_target_id: string;
  staging_deployment_run_id?: string;
  production_deployment_run_id?: string;
  production_rollout_id?: string;
  pipeline_provider: string;
  pipeline_descriptor: Record<string, unknown>;
  pipeline_verdict: Record<string, unknown>;
  gate_result: Record<string, unknown>;
  status: string;
  failure_reason?: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  completed_at?: string;
  staging_deployment_run?: DeploymentRunSummary;
  production_deployment_run?: DeploymentRunSummary;
  production_rollout?: DeploymentRolloutSummary;
  timeline: Array<Record<string, unknown>>;
};

export type DeploymentPromotionListResponse = {
  project_id: string;
  items: DeploymentPromotionSummary[];
};

export type ChangeSetOperationRequest = {
  skill_id: string;
  action: string;
  resource_type: string;
  resource_ref?: string;
  desired_state?: Record<string, unknown>;
  risk_tier?: number;
  destructive?: boolean;
  production_affecting?: boolean;
};

export type ChangeSetSummary = {
  id: string;
  project_id: string;
  provider_account_id?: string;
  title: string;
  summary: string;
  status: string;
  risk_tier: number;
  operations: Array<Record<string, unknown>>;
  expected_cost_usd_micros: number;
  cost_quote: Record<string, unknown>;
  reservation_id?: string;
  approval_decision_id?: string;
  rollback_plan: string[];
  expected_receipts: Array<Record<string, unknown>>;
  policy_result: Record<string, unknown>;
  created_by: string;
  approved_by?: string;
  approved_at?: string;
  applied_by?: string;
  applied_at?: string;
  failure_reason?: string;
  created_at: string;
  updated_at: string;
};

export type ChangeSetListResponse = {
  project_id: string;
  items: ChangeSetSummary[];
};

export type ChangeSetCreateRequest = {
  project_id: string;
  provider_account_id?: string;
  title: string;
  summary: string;
  operations: ChangeSetOperationRequest[];
  expected_cost_usd_micros?: number;
  reservation_id?: string;
  rollback_plan?: string[];
  risk_tier?: number;
  approval_required?: boolean;
  actor?: string;
};

export type ChangeSetApproveRequest = {
  actor?: string;
};

export type ChangeSetApplyRequest = {
  actor?: string;
  created_by_job_id?: string;
  dry_run?: boolean;
};

export type ResourceReceiptSummary = {
  id: string;
  project_id: string;
  changeset_id?: string;
  operation_id?: string;
  provider_account_id?: string;
  receipt_id?: string;
  dedupe_key: string;
  provider: string;
  action: string;
  resource_type: string;
  resource_ref?: string;
  status: string;
  provider_ref?: string;
  request_hash: string;
  result?: Record<string, unknown>;
  error?: Record<string, unknown>;
  created_by_job_id?: string;
  created_at: string;
  updated_at: string;
};

export type ResourceReceiptListResponse = {
  project_id: string;
  items: ResourceReceiptSummary[];
  limit: number;
  offset: number;
};

export type WorkerPoolSummary = {
  id: string;
  project_id: string;
  pool_key: string;
  kind: string;
  provider_account_id?: string;
  provider?: string;
  status: string;
  current_workers: number;
  desired_workers: number;
  min_workers: number;
  max_workers: number;
  max_queue_depth_per_worker: number;
  cost_per_worker_hour_usd_micros: number;
  capabilities: string[];
  metadata: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type WorkerPoolListResponse = {
  project_id: string;
  items: WorkerPoolSummary[];
};

export type WorkerPoolUpsertRequest = {
  project_id: string;
  pool_key: string;
  kind?: string;
  provider_account_id?: string;
  current_workers?: number;
  desired_workers?: number;
  min_workers?: number;
  max_workers?: number;
  max_queue_depth_per_worker?: number;
  cost_per_worker_hour_usd_micros?: number;
  capabilities?: string[];
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type ScalePlanSummary = {
  id: string;
  project_id: string;
  worker_pool_id?: string;
  status: string;
  trigger_reason: string;
  queue_depth: number;
  running_jobs: number;
  leased_jobs: number;
  retryable_jobs: number;
  current_workers: number;
  desired_workers_before: number;
  desired_workers_after: number;
  target_workers: number;
  budget_state: string;
  budget_available_usd_micros: number;
  estimated_hourly_delta_usd_micros: number;
  provider_health_status?: string;
  actions: Array<Record<string, unknown>>;
  recommendations: Array<Record<string, unknown>>;
  changeset_id?: string;
  decision_id?: string;
  created_by: string;
  created_at: string;
  applied_at?: string;
};

export type ScalePlanListResponse = {
  project_id: string;
  items: ScalePlanSummary[];
};

export type ScalePlanRunRequest = {
  project_id: string;
  worker_pool_id?: string;
  cloud_spillover_enabled?: boolean;
  actor?: string;
};

export type ScalePlanRunResponse = {
  scale_plan: ScalePlanSummary;
  worker_pool: WorkerPoolSummary;
  budget_state: string;
  queue: Record<string, number>;
  provider_health: Record<string, unknown>;
  changeset_id?: string;
  decision_id?: string;
};

export type ResourceJanitorRunRequest = {
  project_id: string;
  provider_account_id?: string;
  max_candidates?: number;
  actor?: string;
};

export type ResourceJanitorCandidateSummary = {
  external_resource_id: string;
  provider_account_id?: string;
  provider: string;
  resource_type: string;
  external_id: string;
  name: string;
  environment: string;
  ownership: string;
  health_status: string;
  estimated_monthly_savings_usd_micros: number;
  cost_confidence: string;
  cleanup_skill_id: string;
  destructive: boolean;
};

export type ResourceJanitorSkippedSummary = {
  external_resource_id: string;
  provider: string;
  resource_type?: string;
  external_id: string;
  reason: string;
};

export type ResourceJanitorRunResponse = {
  status: string;
  project_id: string;
  artifact_id: string;
  candidate_count: number;
  skipped_count: number;
  estimated_monthly_savings_usd_micros: number;
  candidates: ResourceJanitorCandidateSummary[];
  skipped: ResourceJanitorSkippedSummary[];
  changesets: ChangeSetSummary[];
};

export type ChangeSetApplyResponse = {
  status: string;
  changeset: ChangeSetSummary;
  resource_receipts: ResourceReceiptSummary[];
  expected_receipts?: Array<Record<string, unknown>>;
  decision_id?: string;
};

export type DeadletterItem = {
  id: string;
  project_id?: string;
  source_kind: string;
  source_id: string;
  related_entity_type?: string;
  related_entity_id?: string;
  related_status?: string;
  reason: string;
  payload: Record<string, unknown>;
  status: string;
  incident_task_id?: string;
  incident_artifact_id?: string;
  remediation_decision_id?: string;
  requeued_job_id?: string;
  resolved_by?: string;
  resolved_at?: string;
  created_at: string;
  updated_at?: string;
};

export type DeadletterListResponse = {
  project_id: string;
  items: DeadletterItem[];
  limit: number;
  offset: number;
};

export type DeadletterIncidentRequest = {
  actor?: string;
  severity?: "low" | "medium" | "high" | "critical";
  summary?: string;
};

export type DeadletterRemediationAction = "retry" | "requeue" | "escalate" | "hold";

export type DeadletterRemediationItem = {
  id: string;
  project_id: string;
  deadletter_id: string;
  action: DeadletterRemediationAction | string;
  status: "queued" | "applied" | "blocked" | "failed" | string;
  actor: string;
  reason?: string;
  result?: Record<string, unknown>;
  requeued_job_id?: string | null;
  decision_id?: string | null;
  error?: string | null;
  created_at: string;
  updated_at?: string;
};

export type DeadletterRemediationListResponse = {
  project_id: string;
  deadletter_id?: string | null;
  items: DeadletterRemediationItem[];
  actions: DeadletterRemediationAction[];
};

export type DeadletterRemediationCreateRequest = {
  actor?: string;
  action: DeadletterRemediationAction;
  reason?: string;
  run_after_seconds?: number;
};

export type DeadletterRemediationCreateResponse = {
  remediation: DeadletterRemediationItem;
  deadletter_id: string;
  project_id: string;
  status: string;
  requeued_job_id?: string | null;
  deadletter_status?: string;
};

export type DeadletterRetryRequest = {
  actor?: string;
  reason?: string;
  run_after_seconds?: number;
};

export type DeadletterIncidentResponse = {
  status: string;
  deadletter: DeadletterItem;
  incident_task_id: string;
  incident_artifact_id: string;
};

export type DeadletterRequeueRequest = {
  actor?: string;
  reason?: string;
  run_after_seconds?: number;
};

export type DeadletterRequeueResponse = {
  status: string;
  deadletter: DeadletterItem;
  requeued_job_id: string;
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
  limit: number;
  offset: number;
};

export type WorkflowDefinitionSummary = {
  id: string;
  project_id: string;
  workflow_key: string;
  name: string;
  description?: string;
  status: string;
  active_version_id?: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  version_count?: number;
};

export type WorkflowVersionSummary = {
  id: string;
  project_id: string;
  workflow_definition_id: string;
  version: number;
  status: string;
  spec: Record<string, unknown>;
  content_hash: string;
  created_by: string;
  created_at: string;
  published_at: string;
  duplicate?: boolean;
};

export type WorkflowDefinitionDetail = WorkflowDefinitionSummary & {
  versions: WorkflowVersionSummary[];
};

export type WorkflowMappingSummary = {
  id: string;
  project_id: string;
  workflow_version_id: string;
  selector_kind: string;
  selector_value: string;
  scope_type: string;
  scope_id: string;
  priority: number;
  enabled: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type WorkflowStepRunSummary = {
  id: string;
  project_id: string;
  workflow_run_id: string;
  step_id: string;
  step_kind: string;
  iteration: number;
  status: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  binding_snapshot: Record<string, unknown>;
  evidence: Record<string, unknown>[];
  attempt: number;
  max_attempts: number;
  run_after: string;
  lease_owner?: string;
  lease_expires_at?: string;
  attempt_timeout_at?: string;
  heartbeat_at?: string;
  decision_id?: string;
  child_workflow_run_id?: string;
  last_error?: string;
  started_at?: string;
  completed_at?: string;
  created_at: string;
  updated_at: string;
};

export type WorkflowRunSummary = {
  id: string;
  project_id: string;
  workflow_definition_id: string;
  workflow_version_id: string;
  workflow_mapping_id?: string;
  parent_run_id?: string;
  root_run_id?: string;
  actor: string;
  status: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  context: Record<string, unknown>;
  policy_snapshot: Record<string, unknown>;
  current_frontier: Record<string, unknown>[];
  dedupe_key?: string;
  blocked_reason?: string;
  error?: string;
  paused_at?: string;
  completed_at?: string;
  created_at: string;
  updated_at: string;
  workflow_key: string;
  workflow_name: string;
  version: number;
  duplicate?: boolean;
  /** "test" for a test run: pinned steps did not execute, and a step that could spend or reach
   *  outside BOSS was refused unless named in `allow_live`. Left out of run lists by default. */
  mode?: WorkflowRunMode;
  /** What each pinned step was settled with, and where the output came from. */
  pins?: Record<string, WorkflowStepPin>;
  allow_live?: string[];
  /** Set when this run was started from a step of an earlier run. */
  rerun_of?: { run_id: string; from_step: string } | null;
};

export type WorkflowRunMode = "normal" | "test";

export type WorkflowStepPin = {
  output: Record<string, unknown>;
  source:
    | { kind: "person"; actor: string }
    | { kind: "rerun"; run_id: string; step_run_id: string; iteration: number };
};

export type WorkflowTestRunRequest = {
  project_id: string;
  workflow_version_id?: string;
  workflow_key?: string;
  version?: number;
  input?: Record<string, unknown>;
  /** step id -> the output that step returns instead of running. */
  pins?: Record<string, Record<string, unknown>>;
  /** Steps that run for real, under every approval and budget rule a normal run has. */
  allow_live?: string[];
  dedupe_key?: string;
  actor?: string;
};

export type WorkflowRunDetail = WorkflowRunSummary & {
  steps: WorkflowStepRunSummary[];
  migrations: Record<string, unknown>[];
  compensations: Record<string, unknown>[];
  timeline: Array<{
    id: string;
    event_type: string;
    actor: string;
    data: Record<string, unknown>;
    created_at: string;
  }>;
};

export type WorkflowDefinitionListResponse = {
  project_id: string;
  items: WorkflowDefinitionSummary[];
};

export type WorkflowMappingListResponse = {
  project_id: string;
  items: WorkflowMappingSummary[];
};

export type WorkflowRunListResponse = {
  project_id: string;
  items: WorkflowRunSummary[];
};

export type AgentStudioCatalogResponse = {
  project_id: string;
  agents: Array<Record<string, unknown>>;
  panels: Array<Record<string, unknown>>;
  workflows: WorkflowDefinitionDetail[];
  agent_runs: Array<Record<string, unknown>>;
  panel_runs: Array<Record<string, unknown>>;
  workflow_runs: WorkflowRunSummary[];
  tests: Array<Record<string, unknown>>;
  a2a: {
    channels: Array<Record<string, unknown>>;
    subscriptions: Array<Record<string, unknown>>;
    messages: Array<Record<string, unknown>>;
    health: Record<string, unknown>;
  };
};

export type A2AListResponse = {
  project_id: string;
  items: Array<Record<string, unknown>>;
};

export type HardwareAgentState = "installed" | "absent";

export type HardwarePlace = {
  id: string;
  kind: string;
  name: string;
  hostname?: string | null;
  arch?: string | null;
  os?: string | null;
  provider?: string | null;
  secret_ref?: string | null;
  /** Present on every place, including the computer where BOSS was installed. */
  agent_state?: HardwareAgentState | null;
};

export type HardwarePlacesResponse = {
  places: HardwarePlace[];
  default_place_id: string | null;
};

export type HardwareLocalPlaceRequest = {
  project_id: string;
  actor: string;
  hostname: string;
  arch: string;
  os: string;
};

export type HardwareAgentEnrollRequest = {
  project_id: string;
  actor: string;
  name: string;
};

/** `enroll_command` is the live API name. `command` is accepted from older callers. */
export type HardwareAgentEnrollResponse = {
  command?: string;
  enroll_command?: string;
  place?: HardwarePlace;
};

export type HardwareProviderPlaceRequest = {
  project_id: string;
  actor: string;
  name: string;
  provider: string;
  secret_ref: string;
};

/** A workflow's webhook trigger (#512). Never carries the signing secret. */
export type WorkflowWebhookTrigger = {
  id: string;
  project_id: string;
  workflow_key: string;
  name: string;
  /** POST signed JSON here (relative to the API base). */
  path: string;
  enabled: boolean;
  secret_last4?: string | null;
  secret_rotated_at?: string | null;
  /** Refusals past the per-minute recording limit: counted, not itemised. */
  refused_unrecorded: number;
  /** Why BOSS turned it off ("Renew authority: …"), cleared when it is turned back on. */
  last_error?: string | null;
  on_behalf_of?: string | null;
  last_delivery_at?: string | null;
  created_at?: string | null;
  actor: string;
};

/** Returned by create and rotate ONLY: the one time the signing secret is shown. */
export type WorkflowWebhookTriggerWithSecret = WorkflowWebhookTrigger & { secret: string };

export type WorkflowWebhookTriggerCreateRequest = {
  project_id: string;
  workflow_key: string;
  name?: string;
  actor?: string;
};

export type WorkflowWebhookDeliveryStatus = "started" | "duplicate" | "refused";

export type WorkflowWebhookDelivery = {
  id: string;
  delivery_id?: string | null;
  status: WorkflowWebhookDeliveryStatus;
  /** Why a refused delivery was refused: bad_signature, stale_timestamp, too_large, disabled… */
  reason?: string | null;
  run_id?: string | null;
  body_bytes?: number | null;
  received_at: string;
};

export type WorkflowWebhookDeliveryList = {
  trigger: WorkflowWebhookTrigger;
  deliveries: WorkflowWebhookDelivery[];
};

// ── KNOWLEDGE GRAPH VIEW (docs/product/KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3; Phase 0 contract) ───
//
// The wire shapes of /v1/knowledge/view/* (routers knowledge_view.py, knowledge_rules.py,
// knowledge_learning.py) and the project archive (projects_archive.py). FROZEN by Phase 0 so the
// read path (S1), rules (S2a), learning (S2b) and the Graph area (S4) build against one shape.
// apps/web/lib/kg-view-types.ts re-exports these; apps/web/tests/fixtures/kg-*.json are real
// northpeak-bank-b (Fineract) rows in exactly these shapes. Change a shape here, the fixtures
// and their test together, never one alone.

/** The product layer (spec §2.1): what a person sees on a chip. Distinct from the storage layer
 *  (`kg_nodes.knowledge_layer`). `domain` is reserved and never written in this build. */
export type KgProductLayer = "code" | "rules" | "learning" | "domain";
export type KgTrustTier = "observed" | "governed" | "inferred" | "approved";
/** `class` covers the type kinds the index records as `attrs.type_kind`: the payload carries the
 *  type kind (interface, enum, record) so the canvas can colour it. */
export type KgSymbolKind = "class" | "interface" | "enum" | "record" | "method" | "function";
export type KgRuleStatus = "proposed" | "approved" | "rejected" | "superseded";
export type KgRuleCategory =
  | "validation" | "calculation" | "limit" | "state_transition" | "authorization" | "other";
/** Learned items: written by agents while they work (spec §4.1). Never a re-index. */
export type KgLearningKind =
  | "investigation" | "test_failure" | "fix" | "workaround" | "review_finding"
  | "decision" | "root_cause" | "precedent" | "fact";
/** Anchors learned items hang on. Drawn, never counted as "learned". */
export type KgAnchorKind =
  | "run" | "incident" | "pull_request" | "release" | "spec" | "runbook" | "alert_rule" | "topic";
/** Rollup levels (kg_view_tree.level). */
export type KgViewLevel = "area" | "package" | "class" | "method";
/** Rollup edge kinds: calls = calls+dispatches_to, extends = extends+implements. */
export type KgRollupEdgeKind = "calls" | "imports" | "extends";
/** Edge kinds a view can return: the rollup kinds plus every kg_edges kind, new ones included. */
export type KgViewEdgeKind =
  | KgRollupEdgeKind
  | "dispatches_to" | "overrides" | "implements" | "references" | "defines" | "exposes"
  | "reads" | "writes" | "about" | "changed" | "evidenced_by" | "approved_by" | "supersedes"
  | "caused_by" | "fixed_by"
  | "consulted" | "reused" | "proposed_by" | "produced" | "ruled_out" | "corrects" | "governs";

/** A node as every view route returns it (spec §3). For rollup nodes (`kind` area | package) and
 *  the two layer bubbles (`kind` layer) `id` is the rollup key (`area:<repo>:<area>`,
 *  `pkg:<repo>:<area>:<package>` — one package name spans areas, 189 of 1,151 on Fineract —
 *  `layer:rules`, `layer:learning`); otherwise it is kg_nodes.id. */
export type KgViewNode = {
  id: string;
  key: string;
  /** kg_nodes.kind, or one of the view-only kinds area · package · layer. */
  kind: string;
  symbol_kind?: KgSymbolKind;
  layer: KgProductLayer;
  label: string;
  /** One line under the label: a path, "869 classes", "approved · validation". */
  sub?: string;
  trust_tier: KgTrustTier;
  /** Rules only. */
  status?: KgRuleStatus;
  /** Area/package: classes, methods, packages. Layer bubbles: per status or per kind. */
  counts?: Record<string, number>;
};

export type KgViewEdge = {
  src: string;
  dst: string;
  kind: KgViewEdgeKind;
  weight: number;
  /** Pre-formatted by the API ("16,749 calls"); null when weight is 1 and there is nothing to say. */
  label: string | null;
};

/** GET /v1/knowledge/view/overview?project_id&actor&layers=code,rules,learning&repo=&area=
 *  With `area`, the package-level overview of that area (breadcrumb "Repo › area"). */
export type KgOverviewResponse = {
  project_id: string;
  repo: string | null;
  /** Every indexed repo of the project, for the picker (shown when > 1). */
  repos: string[];
  level: "area" | "package";
  /** The area being drilled into at level package; null at level area. */
  parent: string | null;
  areas: KgViewNode[];
  /** The Rules and Learning bubbles (kind layer); omitted layers are not in `layers=`. */
  layers: KgViewNode[];
  edges: KgViewEdge[];
  /** Live code-node and edge totals ("Reading 51,319 nodes…"). */
  totals: { nodes: number; edges: number };
  /** kg_view_sync.built_at; null when the rollup was never built. */
  built_at: string | null;
  /** The rollup's node count differs from the live code count: show "Rebuild". */
  stale: boolean;
};

/** A node in a neighbourhood carries its hop from the centre (0 = centre) for the radial layout. */
export type KgNeighbourNode = KgViewNode & { hop: number };

/** GET /v1/knowledge/view/neighbourhood?project_id&actor&node=&depth=1..3&layers=&limit=80 */
export type KgNeighbourhoodResponse = {
  center: KgNeighbourNode;
  nodes: KgNeighbourNode[];
  edges: KgViewEdge[];
  depth: number;
  limit: number;
  /** The level walked: a rollup level for area/package/class, "method" for kg_edges walks,
   *  "graph" for rule and learning nodes (kg_edges of any kind). */
  level: KgViewLevel | "graph";
  truncated: boolean;
  /** What the limit left out: "Showing 80 of 180 connected classes". */
  hidden: { nodes: number; edges: number };
};

export type KgProvenanceEvent = { at: string; what: string; by: string | null;
  /** The person's display name when `by` is a person (provenance names people, never ids). */
  by_name?: string | null };

export type KgProvenance = {
  /** kg_nodes.created_by: an agent role key, a person id, or "system" for the code index. */
  created_by: string;
  created_by_kind: "agent" | "person" | "system";
  agent_role?: string | null;
  agent_name?: string | null;
  /** The agent's Passport id (roles.agent_id); absent for BOSS itself ("BOSS (general)"). */
  agent_id?: string | null;
  /** Who added it, in words (view._who): the code index for code, the agent's name, "BOSS
   *  (general)", or "an agent (not recorded)" for an item written before names were kept. */
  who?: string | null;
  run_id?: string | null;
  workflow_run_id?: string | null;
  code_task_run_id?: string | null;
  step?: string | null;
  model_id?: string | null;
  at: string | null;
  approved_by?: string | null;
  /** The approving person's display name, recorded with the approval. */
  approved_by_name?: string | null;
  approved_at?: string | null;
  version?: number | null;
  previous_id?: string | null;
  /** Shown as a timeline when there is more than one. */
  events: KgProvenanceEvent[];
};

export type KgConnectionGroup = {
  kind: KgViewEdgeKind;
  direction: "out" | "in";
  /** "Calls" · "Called by" · "Evidence" · "Consulted by". */
  label: string;
  total: number;
  /** The first 20; the next page is GET /node/{id}?edge=<kind>&direction=<dir>&offset=20. */
  items: KgViewNode[];
};

export type KgVectorInfo =
  | {
      embedded: true;
      model_id: string;
      dimensions: number;
      neighbours: Array<{ node: KgViewNode; score: number }>;
    }
  | {
      embedded: false;
      /** "Not embedded: code symbols are matched by structure, not meaning". */
      reason: string;
    };

export type KgLineageUse = {
  run_id: string;
  agent_role: string | null;
  agent_name: string | null;
  how: "consulted" | "cited" | "reused";
  at: string;
};

/** "Learned by Responder in run A (step investigate) → used by Builder in run B". */
export type KgLineage = {
  learned: { run_id: string | null; agent_role: string | null; agent_name: string | null; step: string | null; at: string | null };
  used: KgLineageUse[];
};

/** GET /v1/knowledge/view/node/{id}?project_id&actor (&edge&direction&offset for a page). */
export type KgNodeDetailResponse = {
  node: KgViewNode;
  /** kg_nodes.body: the rule statement or the learned statement; null for code. */
  statement: string | null;
  attrs: Record<string, unknown>;
  repo: string | null;
  path: string | null;
  line: number | null;
  end_line: number | null;
  area: string | null;
  package: string | null;
  language: string | null;
  connections: KgConnectionGroup[];
  vector: KgVectorInfo;
  usage: { shown: number; consulted_runs: number; reused_runs: number };
  provenance: KgProvenance;
  /** Learned items only. */
  lineage: KgLineage | null;
};

export type KgTreeTab = "project" | "rules" | "learning";

/** A tree row. Project tab: rollup rows. Rules tab: a status group → a category group → a rule.
 *  Learning tab: a kind group → an item, newest first. */
export type KgTreeRow = {
  key: string;
  parent_key: string | null;
  level: KgViewLevel | "group" | "item";
  /** kg_nodes.id for class, method, rule and learned rows; null for areas, packages and groups. */
  node_id: string | null;
  label: string;
  /** The kind dot: a symbol kind, a rule, a learning kind or an anchor kind. */
  kind: KgSymbolKind | "area" | "package" | "rule" | KgLearningKind | KgAnchorKind | "group";
  layer: KgProductLayer;
  /** Right-aligned count (children, or members of a class). */
  count: number;
  has_children: boolean;
  /** With `q`: true for a match, false for an ancestor shown so the tree opens to it. */
  match?: boolean;
};

/** GET /v1/knowledge/view/tree/children?project_id&actor&parent=&tab=&q=&offset= */
export type KgTreeChildrenResponse = {
  tab: KgTreeTab;
  parent: string | null;
  q: string | null;
  rows: KgTreeRow[];
  total: number;
  /** Paged 200; null at the end. */
  next_offset: number | null;
};

export type KgSearchHit = {
  node: KgViewNode;
  path: string | null;
  /** Why it ranked: exact name > prefix of the last segment > text rank; "meaning" with meaning=1. */
  match: "exact" | "prefix" | "text" | "meaning";
  score: number;
};

/** GET /v1/knowledge/view/search?project_id&actor&q=&layers=&limit=20&meaning=0|1 */
export type KgSearchResponse = {
  q: string;
  hits: KgSearchHit[];
  meaning: boolean;
  /** An embedding model is configured: show "Search by meaning". */
  meaning_available: boolean;
};

export type KgRuleScope = { repo: string | null; areas: string[]; packages: string[]; symbols: string[] };

export type KgRuleEvidence = {
  symbol_id: string;
  symbol_key: string;
  label: string;
  path: string | null;
  /** The cited lines, e.g. [1569, 1576]. */
  lines: [number, number] | null;
  excerpt: string | null;
};

export type KgRuleReview = { by: string; at: string; decision_id: string | null; note: string | null;
  /** The reviewer's display name (null on records made before names were kept). */
  by_name?: string | null };

export type KgRule = {
  node: KgViewNode;
  title: string;
  statement: string;
  rationale: string | null;
  category: KgRuleCategory;
  scope: KgRuleScope;
  status: KgRuleStatus;
  confidence: number;
  provenance: KgProvenance;
  review: KgRuleReview | null;
  version: number;
  previous_id: string | null;
  evidence: KgRuleEvidence[];
  /** The scan that proposed it, for Rules review filtered by scan_id. */
  scan_id: string | null;
};

/** GET /v1/knowledge/view/rules?project_id&actor&status=proposed&scan_id=&area= */
export type KgRulesResponse = {
  items: KgRule[];
  counts: Record<KgRuleStatus, number>;
  scan_id: string | null;
  /** Every scan that proposed rules here, by what it read and when (never its raw id). */
  scans?: { id: string; label: string; rules: number }[];
};

/** POST /v1/knowledge/view/rules/{id}/approve · /edit · /reject (needs knowledge:review). */
export type KgRuleActionRequest = {
  project_id: string;
  actor?: string;
  step_up: "confirmed";
  note?: string;
  /** Edit only: the new version's fields (version n+1, supersedes the old, approved in the act). */
  statement?: string;
  title?: string;
  category?: KgRuleCategory;
  scope?: Partial<KgRuleScope>;
};

export type KgRuleActionResponse = {
  rule: KgRule;
  /** Edit: the version it superseded. */
  previous: KgRule | null;
};

export type KgLearningEvidence = { type: "log" | "test" | "diff" | "metric" | "file"; ref: string; excerpt: string | null };

/** A learned item (spec §2.3 envelope). `detail` holds the kind-specific fields. */
export type KgLearningItem = {
  node: KgViewNode;
  learning_kind: KgLearningKind;
  title: string;
  statement: string;
  detail: Record<string, unknown>;
  about: { symbols: string[]; rules: string[]; areas: string[] };
  evidence: KgLearningEvidence[];
  confidence: number;
  seen_count: number;
  last_seen_run: string | null;
  provenance: KgProvenance;
  at: string;
};

/** A consulted/reused/produced edge that appeared since the cursor. */
export type KgLiveEdge = {
  src: string;
  dst: string;
  kind: "consulted" | "reused" | "produced" | "governs" | "approved_by" | "corrects";
  run_id: string | null;
  how?: "pack" | "tool" | "cited" | null;
  at: string;
};

/** GET /v1/knowledge/view/live?project_id&actor&since=<cursor>&run= */
export type KgLiveResponse = {
  /** Opaque; pass back as `since`. */
  cursor: string;
  learnings: KgLearningItem[];
  rules: KgRule[];
  edges: KgLiveEdge[];
  /** Runs writing to the graph now; the area polls (every poll_after_ms) only while > 0. */
  active_runs: number;
  poll_after_ms: number;
};

/** An item a run consulted or reused, with where it was learned (spec §4.6). */
export type KgRunProofItem = {
  id: string;
  kind: string;
  title: string;
  how: "pack" | "tool" | "cited" | null;
  /** The run that learned it (its produced edge); null for a rule. */
  learned_in: string | null;
  learned_by: string | null;
};

/** One run's scorecard: what it used, and what it cost. */
export type KgRunProof = {
  run_id: string;
  status: string | null;
  /** The project's knowledge_graph switch now: on (arm A) | keyword (arm A′) | off. */
  knowledge_graph: "on" | "keyword" | "off";
  /** Model calls (llm_calls rows) across the run and the builds it started. */
  steps: number;
  turns: number;
  tokens: number;
  cost_usd: number;
  wall_seconds: number | null;
  repair_rounds: number;
  /** Learned items the run produced. */
  learned: number;
  consulted: KgRunProofItem[];
  reused: KgRunProofItem[];
  /** A conversation (run_id athr_…: an agent asked in chat) is scored too: steps are its tool
   *  calls, turns its replies; tokens/cost only when its model calls were metered. */
  kind?: "conversation";
  title?: string;
  agent_role?: string | null;
  agent_name?: string | null;
  started_at?: string | null;
  tokens_measured?: boolean;
  /** From the question to the root cause the conversation recorded. */
  time_to_root_cause_seconds?: number | null;
  tool_calls?: number;
  knowledge_calls?: { tool: string; ok: boolean; query: string | null; refs: string[] }[];
};

export type KgRunComparison = {
  run: KgRunProof;
  baseline: KgRunProof;
  /** run − baseline, per metric; negative is better. */
  delta: Record<"steps" | "turns" | "tokens" | "cost_usd" | "wall_seconds" | "repair_rounds", number | null>
    & { time_to_root_cause_seconds?: number | null };
  /** What `run` consulted or reused that `baseline` learned. */
  carried: KgRunProofItem[];
  /** Better, AND it carried the edge to the baseline's learning (the claim rule). */
  attributed: boolean;
  verdict: string;
};

/** GET /v1/knowledge/view/proof?project_id&actor&run=&baseline= */
export type KgRunProofResponse = { run: KgRunProof | null; comparison: KgRunComparison | null };

/** GET /v1/knowledge/setup?project_id&actor — a knowledge-graph project's guided setup
 *  (engine services/knowledge/project_setup.py): each step's state with its real counts. */
export type KgSetupStepKey = "import" | "team" | "scan" | "review";
export type KgSetupStepState = "done" | "working" | "current" | "waiting";
export type KgSetupStep = { key: KgSetupStepKey; state: KgSetupStepState; done: boolean; summary: string };
export type KgSetupRepository = {
  repo: string; branch?: string | null; status: string; summary?: string | null; error?: string | null;
  files?: number | null; parsed?: number | null; indexed_at?: string | null; graph?: { nodes: number; edges: number } | null;
};
export type KgSetupCandidate = {
  role_key: string; display_name: string;
  /** Its job in a knowledge-graph project (scans, changes, incidents), or null. */
  job: string | null; does: string | null; recommended: boolean;
  mission?: string | null; trust: number; runs: number; autonomy: number;
  /** The first characters of the agent's Passport fingerprint. */
  passport?: string | null;
};
export type KgSetupArea = { key: string; label: string; repo: string; classes: number; packages: number };
export type KgSetupResponse = {
  project_id: string; name: string; project_type: ProjectType; archived: boolean; has_graph: boolean;
  steps: KgSetupStep[]; next: KgSetupStepKey | null; done_count: number;
  repositories: KgSetupRepository[]; code_nodes: number;
  team: { chosen: boolean; role_keys: string[]; candidates: KgSetupCandidate[] };
  scans: { count: number; running: number; completed: number;
           latest: { run_id: string; status: string; at: string | null; scope: unknown } | null };
  scan_defaults: { max_files: number; max_rules: number; budget_usd: number };
  areas: KgSetupArea[];
  rules: { proposed: number; approved: number; rejected: number };
  learnings: number;
  tools?: Record<string, string>;
};

/** POST /v1/projects/{id}/archive · /unarchive (project:delete, step-up confirmed). */
export type ProjectArchiveRequest = { actor?: string; step_up: "confirmed" };
export type ProjectArchiveResponse = {
  project: { id: string; name: string; slug: string; archived_at: string | null; archived_by: string | null };
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
  /** Deadline for every request. Defaults to 120s; never unbounded. */
  timeoutMs?: number;
  apiKey?: string;
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

/**
 * Turn a failed response into a sentence an operator can act on.
 *
 * The API already answers "why" carefully — a blocked preview returns message, run_status,
 * run_phase and next_action. All of it was being discarded in favour of "BOSS API request
 * failed: 409", so the console showed a bare status code for a fault the server had fully
 * explained. Callers render `error.message`, so the explanation has to live there.
 */
export function describeApiFailure(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | undefined)?.detail ?? body;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    const parts = [d.message, d.next_action]
      .filter((v): v is string => typeof v === "string" && v.trim().length > 0);
    if (parts.length) return parts.join(" ");
    if (typeof d.error === "string" && d.error.trim()) return d.error;
  }
  return `BOSS API request failed: ${status}`;
}

/** Shared bounded HTTP transport for the Community and private extensions. */
export function createBossRequest(config: BossApiClientConfig) {

  const baseUrl = config.baseUrl.replace(/\/$/, "");
  const fetchImpl = config.fetchImpl ?? fetch;
  // Generous: some control-plane calls legitimately take tens of seconds (a promote
  // measured 8.4s on a warm API). The point is a bound, not a tight one.
  const timeoutMs = config.timeoutMs ?? 120_000;
  const apiKey = config.apiKey?.trim();

  // A GET THAT WRITES is called by its POST twin, `<path>/load`, with the same query (the API's
  // identity_guard.writing_get): a cookie-authenticated GET to one is refused without CSRF, and a
  // server-side GET sends none. apps/web/tests/no-console-caller-gets-a-writing-get.test.ts.
  async function request<T>(
    method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
    path: string,
    options?: {
      query?: Record<string, QueryValue>;
      body?: unknown;
    },
  ): Promise<T> {
    // A REQUEST WITH NO DEADLINE IS A SPINNER WITH NO END.
    //
    // Nothing bounded these fetches. When the API went cold — a first query on reference host took
    // 28.9 seconds — a server action simply never returned, so `pending` never cleared and the
    // operator watched "Convening…" forever for a promote that was still in flight. The action
    // itself was written correctly, with try/catch returning an error either way; it was never
    // reached, because the await had no end. Every one of these buttons has the same shape, so
    // the deadline belongs here rather than at each call site.
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${withQuery(path, options?.query)}`, {
        method,
        cache: "no-store",
        signal: AbortSignal.timeout(timeoutMs),
        headers: {
          Accept: "application/json",
          ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
          ...(options?.body ? { "Content-Type": "application/json" } : {}),
        },
        ...(options?.body ? { body: JSON.stringify(options.body) } : {}),
      });
    } catch (cause) {
      const timedOut = cause instanceof DOMException && cause.name === "TimeoutError";
      throw new BossApiError(
        timedOut
          ? `The control plane did not answer within ${Math.round(timeoutMs / 1000)}s. `
            + "It may still be working — reload to see whether it completed."
          : `Could not reach the control plane: ${cause instanceof Error ? cause.message : String(cause)}`,
        0,
        undefined,
      );
    }

    if (!response.ok) {
      const rawBody = await response.text();
      let body: unknown = rawBody;
      try {
        body = rawBody ? JSON.parse(rawBody) : undefined;
      } catch {}
      throw new BossApiError(
        describeApiFailure(response.status, body),
        response.status,
        body,
      );
    }

    if (response.status === 204) {
      return undefined as T;
    }
    const text = await response.text();
    if (!text) {
      return undefined as T;
    }
    const contentType = response.headers.get("content-type") ?? "";
    if (
      contentType.includes("yaml") ||
      contentType.includes("text/plain") ||
      contentType.includes("vnd.boss.capability")
    ) {
      return text as T;
    }
    return JSON.parse(text) as T;
  }
  return request;
}

export function createBossApiClient(config: BossApiClientConfig) {
  const request = createBossRequest(config);
  return {
    getAssuranceOverview(projectId: string) {
      return request<AssuranceOverviewResponse>("GET", "/v1/assurance/overview", {
        query: { project_id: projectId },
      });
    },
    getModelGatewayUsage(projectId: string, days = 30) {
      return request<ModelGatewayUsageResponse>("GET", "/v1/model-gateway/usage", {
        query: { project_id: projectId, days },
      });
    },
    getAgentStudioCatalog(projectId: string) {
      return request<AgentStudioCatalogResponse>(
        "GET",
        "/v1/agent-studio/catalog",
        { query: { project_id: projectId } },
      );
    },
    seedAgentStudioCatalog(payload: {
      project_id: string;
      actor?: string;
    }) {
      return request<{
        project_id: string;
        status: string;
        agents: Array<Record<string, unknown>>;
        panels: Array<Record<string, unknown>>;
        agent_count: number;
        panel_count: number;
        external_effects: number;
      }>("POST", "/v1/agent-studio/catalog/seed", { body: payload });
    },
    validateStudioWorkflow(payload: {
      project_id: string;
      spec: Record<string, unknown>;
      actor?: string;
    }) {
      return request<{
        valid: boolean;
        normalized_spec: Record<string, unknown>;
        checks: Array<{ kind: string; passed: boolean; detail: string }>;
      }>("POST", "/v1/agent-studio/workflows/validate", { body: payload });
    },
    createAgentDefinition(payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/agent-studio/agents",
        { body: payload },
      );
    },
    createAgentVersion(agentId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/agent-studio/agents/${encodeURIComponent(agentId)}/versions`,
        { body: payload },
      );
    },
    validateAgentVersion(versionId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/agent-studio/agent-versions/${encodeURIComponent(versionId)}/validate`,
        { body: payload },
      );
    },
    publishAgentVersion(versionId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/agent-studio/agent-versions/${encodeURIComponent(versionId)}/publish`,
        { body: payload },
      );
    },
    testAgentVersion(versionId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/agent-studio/agent-versions/${encodeURIComponent(versionId)}/tests`,
        { body: payload },
      );
    },
    runAgentVersion(versionId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/agent-studio/agent-versions/${encodeURIComponent(versionId)}/runs`,
        { body: payload },
      );
    },
    createPanelDefinition(payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/agent-studio/panels",
        { body: payload },
      );
    },
    createPanelVersion(panelId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/agent-studio/panels/${encodeURIComponent(panelId)}/versions`,
        { body: payload },
      );
    },
    publishPanelVersion(versionId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/agent-studio/panel-versions/${encodeURIComponent(versionId)}/publish`,
        { body: payload },
      );
    },
    runPanelVersion(versionId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/agent-studio/panel-versions/${encodeURIComponent(versionId)}/runs`,
        { body: payload },
      );
    },
    listA2AChannels(projectId: string) {
      return request<A2AListResponse>("GET", "/v1/a2a/channels", {
        query: { project_id: projectId },
      });
    },
    createA2AChannel(payload: Record<string, unknown>) {
      return request<Record<string, unknown>>("POST", "/v1/a2a/channels", {
        body: payload,
      });
    },
    createA2ASubscription(payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/a2a/subscriptions",
        { body: payload },
      );
    },
    publishA2AMessage(payload: Record<string, unknown>) {
      return request<Record<string, unknown>>("POST", "/v1/a2a/messages", {
        body: payload,
      });
    },
    cancelA2AMessage(messageId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/a2a/messages/${encodeURIComponent(messageId)}/cancel`,
        { body: payload },
      );
    },
    replayA2AMessage(messageId: string, payload: Record<string, unknown>) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/a2a/messages/${encodeURIComponent(messageId)}/replay`,
        { body: payload },
      );
    },
    listA2AMessages(projectId: string, limit = 100, traceId?: string) {
      return request<A2AListResponse>("GET", "/v1/a2a/messages", {
        query: { project_id: projectId, limit, trace_id: traceId },
      });
    },
    pollA2A(payload: Record<string, unknown>) {
      return request<{ items: Array<Record<string, unknown>> }>(
        "POST",
        "/v1/a2a/poll",
        { body: payload },
      );
    },
    acknowledgeA2ADelivery(
      deliveryId: string,
      payload: Record<string, unknown>,
    ) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/a2a/deliveries/${encodeURIComponent(deliveryId)}/acknowledge`,
        { body: payload },
      );
    },
    getA2AHealth(projectId: string) {
      return request<Record<string, unknown>>("GET", "/v1/a2a/health", {
        query: { project_id: projectId },
      });
    },
    getA2AAudit(projectId: string, limit = 100) {
      return request<Record<string, unknown>>("GET", "/v1/a2a/audit", {
        query: { project_id: projectId, limit },
      });
    },
    getWorkflowDefinitions(projectId: string, includeRetired = false) {
      return request<WorkflowDefinitionListResponse>("GET", "/v1/workflows", {
        query: { project_id: projectId, include_retired: includeRetired },
      });
    },
    createWorkflowDefinition(payload: {
      project_id: string;
      workflow_key: string;
      name: string;
      description?: string;
      actor?: string;
    }) {
      return request<WorkflowDefinitionSummary>("POST", "/v1/workflows", {
        body: payload,
      });
    },
    getWorkflowDefinition(definitionId: string, projectId: string) {
      return request<WorkflowDefinitionDetail>(
        "GET",
        `/v1/workflows/${encodeURIComponent(definitionId)}`,
        { query: { project_id: projectId } },
      );
    },
    publishWorkflowVersion(
      definitionId: string,
      payload: {
        project_id: string;
        spec: Record<string, unknown>;
        activate?: boolean;
        actor?: string;
      },
    ) {
      return request<WorkflowVersionSummary>(
        "POST",
        `/v1/workflows/${encodeURIComponent(definitionId)}/versions`,
        { body: payload },
      );
    },
    getWorkflowMappings(projectId: string) {
      return request<WorkflowMappingListResponse>("GET", "/v1/workflow-mappings", {
        query: { project_id: projectId },
      });
    },
    createWorkflowMapping(payload: {
      project_id: string;
      workflow_version_id: string;
      selector_kind: string;
      selector_value: string;
      scope_type: "org" | "workspace" | "project";
      scope_id: string;
      priority?: number;
      actor?: string;
    }) {
      return request<WorkflowMappingSummary>("POST", "/v1/workflow-mappings", {
        body: payload,
      });
    },
    resolveWorkflowMapping(payload: {
      project_id: string;
      selector_kind: string;
      selector_value: string;
      actor?: string;
    }) {
      return request<{
        status: string;
        selected?: WorkflowMappingSummary & {
          workflow_key: string;
          workflow_name: string;
          version: number;
        };
        rejected: Record<string, unknown>[];
        reason: string;
      }>("POST", "/v1/workflow-mappings/resolve", { body: payload });
    },
    getWorkflowRuns(projectId: string, status?: string, limit = 50) {
      return request<WorkflowRunListResponse>("GET", "/v1/workflow-runs", {
        query: { project_id: projectId, status, limit },
      });
    },
    getWorkflowRun(runId: string, projectId: string) {
      return request<WorkflowRunDetail>(
        "GET",
        `/v1/workflow-runs/${encodeURIComponent(runId)}`,
        { query: { project_id: projectId } },
      );
    },
    startWorkflowRun(payload: {
      project_id: string;
      workflow_version_id?: string;
      selector_kind?: string;
      selector_value?: string;
      input?: Record<string, unknown>;
      policy_snapshot?: Record<string, unknown>;
      dedupe_key: string;
      parent_run_id?: string;
      actor?: string;
    }) {
      return request<WorkflowRunSummary>("POST", "/v1/workflow-runs", {
        body: payload,
      });
    },
    /** Run a saved workflow as a test with pinned step outputs. */
    startWorkflowTestRun(payload: WorkflowTestRunRequest) {
      return request<WorkflowRunSummary>("POST", "/v1/delivery-workflows/test-runs", {
        body: payload,
      });
    },
    getWorkflowTestRuns(projectId: string, limit = 25) {
      return request<{ project_id: string; items: WorkflowRunSummary[] }>(
        "GET", "/v1/delivery-workflows/test-runs", { query: { project_id: projectId, limit } },
      );
    },
    /** Start a new run from `from_step` of a finished or failed run; earlier steps are pinned
     *  to what that run produced. */
    rerunWorkflowFromStep(
      runId: string,
      payload: { project_id: string; from_step: string; dedupe_key?: string; actor?: string },
    ) {
      return request<WorkflowRunSummary>(
        "POST",
        `/v1/delivery-workflows/runs/${encodeURIComponent(runId)}/rerun`,
        { body: payload },
      );
    },
    advanceWorkflowRun(runId: string, projectId: string, actor = "ceo") {
      return request<WorkflowRunDetail>(
        "POST",
        `/v1/workflow-runs/${encodeURIComponent(runId)}/advance`,
        { body: { project_id: projectId, actor } },
      );
    },
    pauseWorkflowRun(runId: string, projectId: string, actor = "ceo") {
      return request<WorkflowRunDetail>(
        "POST",
        `/v1/workflow-runs/${encodeURIComponent(runId)}/pause`,
        { body: { project_id: projectId, actor } },
      );
    },
    resumeWorkflowRun(runId: string, projectId: string, actor = "ceo") {
      return request<WorkflowRunDetail>(
        "POST",
        `/v1/workflow-runs/${encodeURIComponent(runId)}/resume`,
        { body: { project_id: projectId, actor } },
      );
    },
    cancelWorkflowRun(
      runId: string,
      payload: { project_id: string; actor?: string; reason: string; compensate?: boolean },
    ) {
      return request<WorkflowRunDetail>(
        "POST",
        `/v1/workflow-runs/${encodeURIComponent(runId)}/cancel`,
        { body: payload },
      );
    },
    claimWorkflowStep(
      runId: string,
      payload: { project_id: string; worker_id: string; lease_seconds?: number },
    ) {
      return request<WorkflowStepRunSummary | null>(
        "POST",
        `/v1/workflow-runs/${encodeURIComponent(runId)}/steps/claim`,
        { body: payload },
      );
    },
    heartbeatWorkflowStep(
      stepRunId: string,
      payload: { project_id: string; worker_id: string; lease_seconds?: number },
    ) {
      return request<WorkflowStepRunSummary>(
        "POST",
        `/v1/workflow-step-runs/${encodeURIComponent(stepRunId)}/heartbeat`,
        { body: payload },
      );
    },
    completeWorkflowStep(
      stepRunId: string,
      payload: {
        project_id: string;
        worker_id: string;
        output?: Record<string, unknown>;
        evidence?: Record<string, unknown>[];
      },
    ) {
      return request<WorkflowStepRunSummary>(
        "POST",
        `/v1/workflow-step-runs/${encodeURIComponent(stepRunId)}/complete`,
        { body: payload },
      );
    },
    failWorkflowStep(
      stepRunId: string,
      payload: {
        project_id: string;
        worker_id: string;
        error: string;
        retryable?: boolean;
        retry_delay_seconds?: number;
      },
    ) {
      return request<WorkflowStepRunSummary>(
        "POST",
        `/v1/workflow-step-runs/${encodeURIComponent(stepRunId)}/fail`,
        { body: payload },
      );
    },
    getReportSummary(projectId: string) {
      return request<ReportSummaryResponse>("GET", "/v1/reports/summary", {
        query: { project_id: projectId },
      });
    },
    getAgentRuns(projectId: string) {
      return request<AgentRunsResponse>("GET", "/v1/agent-runs", {
        query: { project_id: projectId },
      });
    },
    getTaskOutputs(projectId: string) {
      return request<TaskOutputsResponse>("GET", "/v1/outputs", {
        query: { project_id: projectId },
      });
    },
    getOrgs() {
      return request<OrgsResponse>("GET", "/v1/orgs");
    },
    onboardOrg(name: string) {
      return request<{ org_id: string; project_id: string; name: string }>(
        "POST", "/v1/orgs", { body: { name } },
      );
    },
    getApprovalRules(projectId: string) {
      return request<ApprovalRulesResponse>("GET", "/v1/approval-rules", {
        query: { project_id: projectId },
      });
    },
    getCapabilities(projectId: string) {
      return request<CapabilitiesResponse>("GET", "/v1/capabilities", {
        query: { project_id: projectId },
      });
    },
    getMeetings(projectId: string) {
      return request<MeetingsResponse>("GET", "/v1/meeting-rooms", {
        query: { project_id: projectId },
      });
    },
    getMeetingRoom(meetingId: string, projectId: string) {
      return request<Meeting>("GET", `/v1/meeting-rooms/${encodeURIComponent(meetingId)}`, {
        query: { project_id: projectId },
      });
    },
    getMeetingRoles(projectId: string) {
      return request<MeetingRolesResponse>("POST", "/v1/meeting-roles/load", {
        query: { project_id: projectId },
      });
    },
    upsertMeetingRole(payload: {
      project_id: string; title: string; lens: string; key?: string;
      risk_tier?: number; is_default?: boolean; enabled?: boolean;
    }) {
      return request<{ key: string; title: string }>("POST", "/v1/meeting-roles", { body: payload });
    },
    setMeetingRoleEnabled(roleKey: string, projectId: string, enabled: boolean) {
      return request<{ role_key: string; enabled: boolean }>(
        "PATCH", `/v1/meeting-roles/${encodeURIComponent(roleKey)}`,
        { query: { project_id: projectId, enabled } },
      );
    },
    deleteMeetingRole(roleKey: string, projectId: string) {
      return request<void>("DELETE", `/v1/meeting-roles/${encodeURIComponent(roleKey)}`, {
        query: { project_id: projectId },
      });
    },
    conveneMeeting(payload: MeetingConvenePayload) {
      return request<{ status: string; task_id: string; job_id: string | null; run_id?: string | null; meeting_run_id?: string | null; extends_run_id?: string | null }>(
        "POST", "/v1/meetings/prd", { body: payload },
      );
    },
    getCodeTasks(projectId: string, limit = 50, excludeInternal = false) {
      return request<CodeTasksResponse>("GET", "/v1/code-tasks", {
        // excludeInternal drops BOSS's own maintenance runs BEFORE the limit, so a burst of
        // housekeeping cannot consume the whole window and hide every app an operator built.
        query: { project_id: projectId, limit,
                 ...(excludeInternal ? { exclude_internal: true } : {}) },
      });
    },
    getQaBenchmarkApps(projectId?: string) {
      // project_id is what lets the catalog say which of these already exist here.
      return request<QaBenchmarkAppsResponse>("GET", "/v1/qa/benchmark-apps",
        projectId ? { query: { project_id: projectId } } : undefined);
    },
    startQaBenchmarkApp(benchmarkId: string, payload: {
      project_id: string;
      actor?: string;
      task_id?: string;
      owner?: string;
      repo?: string;
      base_branch?: string;
      profile?: string;
      /** Reportable official canary. Defaults the launch to the direct code-task path. */
      official?: boolean;
      /** Per-run model-engine failover (openai, claude_code, …). */
      engine?: string;
      /** Build a SECOND copy of something already built. Omitted, the API answers with the
       *  existing run instead of spending another full build. */
      force?: boolean;
    }) {
      return request<{
        status: string;
        run_id: string;
        benchmark: { id: string; name: string };
        pull_request_url?: string;
        task_id?: string;
        hint?: string;
      }>(
        "POST", `/v1/qa/benchmark-apps/${encodeURIComponent(benchmarkId)}/runs`, { body: payload },
      );
    },
    getQaBenchmarkReport(runId: string) {
      return request<QaBenchmarkReport>(
        "GET", `/v1/qa/benchmark-apps/runs/${encodeURIComponent(runId)}/report`,
      );
    },
    listQaBenchmarkEvaluations(projectId: string, limit = 50) {
      return request<{ project_id: string; items: QaBenchmarkEvaluation[] }>(
        "GET",
        "/v1/qa/benchmark-apps/evaluations",
        { query: { project_id: projectId, limit } },
      );
    },
    sweepQaBenchmarkEvaluations(payload?: {
      project_id?: string;
      limit?: number;
      actor?: string;
    }) {
      return request<{
        status: string;
        project_id?: string | null;
        checked_count: number;
        changed_count: number;
        ready_for_candidate_count: number;
        items: QaBenchmarkEvaluation[];
      }>("POST", "/v1/qa/benchmark-apps/tuning-sweep", { body: payload ?? {} });
    },
    createQaBenchmarkTuningProposal(
      runId: string,
      payload: QaBenchmarkTuningProposalRequest,
    ) {
      return request<SelfImprovementProposalDetail>(
        "POST",
        `/v1/qa/benchmark-apps/runs/${encodeURIComponent(runId)}/tuning-proposal`,
        { body: payload },
      );
    },
    getDiscoveries(projectId: string, limit = 50) {
      return request<DiscoveriesResponse>("GET", "/v1/discoveries", {
        query: { project_id: projectId, limit },
      });
    },
    getDiscovery(runId: string) {
      return request<DiscoveryRun>("GET", `/v1/discoveries/${encodeURIComponent(runId)}`);
    },
    startDiscovery(payload: { project_id: string; seed: string; title?: string; context?: string;
                              deep?: boolean }) {
      return request<{ status: string; run_id: string }>("POST", "/v1/discoveries", { body: payload });
    },
    promoteDiscoveryOutcome(runId: string, outcomeIndex: number) {
      return request<{ status: string; task_id: string; meeting_run_id: string }>(
        "POST", `/v1/discoveries/${encodeURIComponent(runId)}/promote`,
        { body: { outcome_index: outcomeIndex } });
    },
    deepenDiscovery(runId: string) {
      return request<{ status: string; run_id: string; deep: boolean }>(
        "POST", `/v1/discoveries/${encodeURIComponent(runId)}/deepen`);
    },
    getCodeTask(runId: string) {
      return request<CodeTaskRun>("GET", `/v1/code-tasks/${encodeURIComponent(runId)}`);
    },
    getCodeTaskActivity(runId: string) {
      return request<CodeTaskActivityResponse>(
        "GET", `/v1/code-tasks/${encodeURIComponent(runId)}/activity`);
    },
    createCodeTask(payload: CodeTaskCreatePayload) {
      // `workflow_run_id` is the OUTER run — research → panel → build → preview → deploy —
      // which is where the operator is sent to watch what they just started.
      return request<{ status: string; run_id: string; workflow_run_id?: string | null }>(
        "POST", "/v1/code-tasks", { body: payload });
    },
    retryCodeTask(runId: string, fixNote?: string, actor = "executive_console") {
      return request<{ status: string; run_id: string; attempt: number }>(
        "POST", `/v1/code-tasks/${encodeURIComponent(runId)}/retry`,
        { body: { fix_note: fixNote ?? "", actor } });
    },
    followUpCodeTask(runId: string, instructions: string, actor = "executive_console",
                     title?: string) {
      return request<{ status: string; run_id: string; root_run_id: string;
                       base_branch: string; owner?: string; repo?: string }>(
        "POST", `/v1/code-tasks/${encodeURIComponent(runId)}/follow-up`,
        { body: { instructions, actor, title } });
    },
    submitCodeTaskFeedback(runId: string, payload: {
      message: string;
      expected?: string;
      anchor?: string;
      severity?: string;
      selected_element?: string;
      screenshot_url?: string;
      /** The path the operator chose for this change, from the picker on "Add to this
       *  project". Empty means the change follows the inner pipeline only, as before. */
      workflow_version_id?: string;
    }) {
      return request<FeedbackResponse>(
        "POST", `/v1/code-tasks/${encodeURIComponent(runId)}/feedback`, { body: payload });
    },
    confirmCodeTaskFeedback(threadId: string, actor = "operator") {
      return request<FeedbackResponse>(
        "POST", `/v1/feedback-threads/${encodeURIComponent(threadId)}/confirm`,
        { body: { actor } });
    },
    overrideCodeTask(
      runId: string,
      waiveGates: string[],
      reason?: string,
      actor = "executive_console",
    ) {
      return request<{ status: string; pull_request_url?: string; pull_request_number?: number }>(
        "POST", `/v1/code-tasks/${encodeURIComponent(runId)}/override`,
        { body: { waive_gates: waiveGates, reason: reason ?? "", actor } });
    },
    startPreview(runId: string, ttlMinutes?: number, actor = "executive_console") {
      return request<{
        id: string; status: string; url?: string; error?: string;
        // library/CLI builds have no live URL — they are "run" by executing their tests:
        mode?: string; kind?: string; command?: string; exit_code?: number;
        passed?: boolean; output?: string; message?: string;
      }>(
        "POST", `/v1/code-tasks/${encodeURIComponent(runId)}/preview`,
        { body: { ttl_minutes: ttlMinutes ?? 60, actor } });
    },
    stopPreview(previewId: string, actor = "executive_console") {
      return request<{ status: string }>(
        "POST", `/v1/previews/${encodeURIComponent(previewId)}/stop`,
        { body: { actor } });
    },
    getPreviews(projectId: string, limit = 50) {
      return request<{ previews: PreviewRec[] }>(
        "POST", "/v1/previews/load", { query: { project_id: projectId, limit } });
    },
    getSandboxes() {
      return request<SandboxesResponse>("GET", "/v1/sandboxes");
    },
    getSandboxHelpers() {
      return request<{
        categories: Record<string, string[]>;
        commands: string[];
        unavailable?: string;
      }>("GET", "/v1/sandboxes/helpers");
    },
    getSandboxTree(sandboxId: string) {
      return request<{ tree: string[] }>("GET", `/v1/sandboxes/${encodeURIComponent(sandboxId)}/tree`);
    },
    getSandboxFile(sandboxId: string, path: string) {
      return request<{ path: string; content: string }>(
        "GET", `/v1/sandboxes/${encodeURIComponent(sandboxId)}/file`, { query: { path } });
    },
    execSandbox(sandboxId: string, command: string, breakGlass = false, reason?: string) {
      return request<SandboxExecResult>("POST", `/v1/sandboxes/${encodeURIComponent(sandboxId)}/exec`,
        { body: { command, break_glass: breakGlass, reason } });
    },
    getSandboxStats(sandboxId: string) {
      return request<SandboxStats>("GET", `/v1/sandboxes/${encodeURIComponent(sandboxId)}/stats`);
    },
    getSandboxLogs(sandboxId: string) {
      return request<{ serial_tail?: string; agent_log?: string; unavailable?: string }>(
        "GET", `/v1/sandboxes/${encodeURIComponent(sandboxId)}/logs`);
    },
    holdSandbox(sandboxId: string, hours = 2) {
      return request<Record<string, unknown>>("POST", `/v1/sandboxes/${encodeURIComponent(sandboxId)}/preserve`,
        { body: { hours } });
    },
    preserveSandbox(sandboxId: string, reason: string, actor = "operator") {
      return request<SandboxSummary>("POST", `/v1/sandboxes/${encodeURIComponent(sandboxId)}/preserve`,
        { body: { reason, actor } });
    },
    destroySandbox(sandboxId: string) {
      return request<{ status: string }>("DELETE", `/v1/sandboxes/${encodeURIComponent(sandboxId)}`);
    },
    registerMcpServer(payload: McpServerRegisterPayload) {
      return request<McpServer>("POST", "/v1/mcp/servers", { body: payload });
    },
    setMcpServerEnabled(serverId: string, projectId: string, enabled: boolean) {
      return request<McpServer>("PATCH", `/v1/mcp/servers/${encodeURIComponent(serverId)}`, {
        query: { project_id: projectId, enabled },
      });
    },
    updateMcpServer(serverId: string, projectId: string, changes: McpServerUpdatePayload) {
      return request<McpServer>("PATCH", `/v1/mcp/servers/${encodeURIComponent(serverId)}`, {
        query: { project_id: projectId },
        body: changes,
      });
    },
    deleteMcpServer(serverId: string, projectId: string) {
      return request<void>("DELETE", `/v1/mcp/servers/${encodeURIComponent(serverId)}`, {
        query: { project_id: projectId },
      });
    },
    /** `check`: none (what was recorded), stale (probe servers not checked in the last
     *  minute), now (probe every one). */
    getMcpServers(projectId: string, check: "none" | "stale" | "now" = "none") {
      return request<McpServersResponse>("POST", "/v1/mcp/servers/load", {
        query: { project_id: projectId, check },
      });
    },
    getHealth() {
      return request<HealthResponse>("GET", "/health");
    },
    listProjects() {
      return request<ProjectListResponse>("GET", "/v1/projects");
    },
    createProject(body: { name: string; slug?: string; description?: string; project_type?: ProjectType }) {
      return request<ProjectSummary & { description: string | null }>("POST", "/v1/projects", { body });
    },
    getAuthSession() {
      return request<AuthSessionResponse>("GET", "/v1/auth/session");
    },
    createSamlSession(projectId: string, samlResponse: string) {
      return request<SamlLoginResponse>(
        "POST",
        `/saml/v2/${encodeURIComponent(projectId)}/acs`,
        { body: { SAMLResponse: samlResponse } },
      );
    },
    getEdition(projectId: string) {
      return request<EditionStatusResponse>("GET", "/v1/edition", {
        query: { project_id: projectId },
      });
    },
    getCloudCatalog(projectId: string) {
      return request<{
        project_id: string;
        edition_id: string;
        independence: Record<string, unknown>;
        cloud_features: Array<Record<string, unknown>>;
        enterprise_features: Array<Record<string, unknown>>;
        packaging: Record<string, string>;
      }>("GET", "/v1/cloud/catalog", {
        query: { project_id: projectId },
      });
    },
    getCloudStatus(projectId: string) {
      return request<{
        project_id: string;
        edition: EditionStatusResponse;
        independent: boolean;
        linked: boolean;
        easy_connect: Record<string, unknown>;
        managed_workers?: Record<string, unknown>;
        linked_capabilities?: Record<string, unknown>;
        message: string;
      }>("GET", "/v1/cloud/status", {
        query: { project_id: projectId },
      });
    },
    getCloudComparison(projectId: string) {
      return request<{
        project_id: string;
        edition_id: string;
        linked: boolean;
        rows: Array<Record<string, unknown>>;
        marketing_url: string;
        principle: string;
        connect_path: Record<string, unknown>;
      }>("GET", "/v1/cloud/comparison", {
        query: { project_id: projectId },
      });
    },
    getCloudCapabilities(projectId: string) {
      return request<{
        project_id: string;
        edition_id: string;
        linked: boolean;
        local_capabilities: Array<Record<string, unknown>>;
        cloud_unlocked: Array<Record<string, unknown>>;
        cloud_locked: Array<Record<string, unknown>>;
        enterprise_features: Array<Record<string, unknown>>;
        summary: Record<string, unknown>;
      }>("GET", "/v1/cloud/capabilities", {
        query: { project_id: projectId },
      });
    },
    getCloudWorkers(projectId: string) {
      return request<{
        project_id: string;
        status: string;
        available_now: boolean;
        cloud_eligible: boolean;
        linked: boolean;
        local_enrollments?: Array<Record<string, unknown>>;
        managed_capacity: Record<string, unknown>;
        hybrid?: Record<string, unknown>;
        enrollment: Record<string, unknown>;
      }>("GET", "/v1/cloud/workers", {
        query: { project_id: projectId },
      });
    },
    enrollCloudWorker(payload: {
      project_id: string;
      worker_node_id: string;
      display_name?: string;
      capabilities?: string[];
      metadata?: Record<string, unknown>;
      actor?: string;
    }) {
      return request<{
        id: string;
        project_id: string;
        worker_node_id: string;
        status: string;
        receipt_id?: string | null;
        managed_fleets_available: boolean;
        managed_fleet?: boolean;
        message?: string;
      }>("POST", "/v1/cloud/workers/enroll", { body: payload });
    },
    syncCloudEntitlements(payload: {
      project_id: string;
      hosted_org_id?: string;
      actor?: string;
    }) {
      return request<{
        project_id: string;
        synced_entitlement_count: number;
        edition: EditionStatusResponse;
        linked: boolean;
        blocked_reason?: string | null;
      }>("POST", "/v1/cloud/sync", { body: payload });
    },
    listCloudEntitlements(projectId: string, includeInactive = false) {
      return request<{
        project_id: string;
        linked: boolean;
        edition_id: string;
        items: Array<Record<string, unknown>>;
        local_count: number;
        hosted_count: number;
        blocked_reason?: string | null;
        message?: string;
      }>("GET", "/v1/cloud/entitlements", {
        query: {
          project_id: projectId,
          include_inactive: String(includeInactive),
        },
      });
    },
    createCloudTeamInvite(payload: {
      project_id: string;
      email: string;
      display_name?: string;
      role_keys?: string[];
      permissions?: string[];
      metadata?: Record<string, unknown>;
      actor?: string;
    }) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/cloud/team-invites",
        { body: payload },
      );
    },
    listCloudTeamInvites(projectId: string, limit = 50) {
      return request<{
        project_id: string;
        linked: boolean;
        items: Array<Record<string, unknown>>;
        blocked_reason?: string | null;
      }>("GET", "/v1/cloud/team-invites", {
        query: { project_id: projectId, limit: String(limit) },
      });
    },
    listCloudMarketplace(
      projectId: string,
      options?: { includeInactive?: boolean; limit?: number },
    ) {
      return request<{
        project_id: string;
        linked: boolean;
        items: Array<Record<string, unknown>>;
        blocked_reason?: string | null;
        message?: string | null;
      }>("GET", "/v1/cloud/marketplace", {
        query: {
          project_id: projectId,
          include_inactive: String(options?.includeInactive ?? false),
          limit: String(options?.limit ?? 50),
        },
      });
    },
    startCloudEasyConnect(payload: {
      project_id: string;
      method?: "browser" | "device";
      return_url?: string;
      actor?: string;
    }) {
      return request<{
        session_id: string;
        status: string;
        method: string;
        authorization_url?: string | null;
        user_code?: string | null;
        verification_uri?: string | null;
        device_code?: string | null;
        poll_interval_seconds?: number;
        expires_at?: string | null;
        message?: string | null;
        receipt_id?: string | null;
      }>("POST", "/v1/cloud/easy-connect", { body: payload });
    },
    pollCloudEasyConnect(
      sessionId: string,
      projectId: string,
      actor = "ceo",
    ) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/cloud/easy-connect/${encodeURIComponent(sessionId)}/load`,
        { query: { project_id: projectId, actor } },
      );
    },
    completeCloudEasyConnect(
      sessionId: string,
      payload: {
        project_id: string;
        actor?: string;
        code?: string;
        state?: string;
      },
    ) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/cloud/easy-connect/${encodeURIComponent(sessionId)}/complete`,
        { body: payload },
      );
    },
    completeCloudEasyConnectByState(payload: {
      project_id: string;
      actor?: string;
      code?: string;
      state?: string;
      session_id?: string;
    }) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/cloud/easy-connect/complete",
        { body: payload },
      );
    },
    unlinkCloud(payload: {
      project_id: string;
      actor?: string;
      reason?: string;
    }) {
      return request<{
        project_id: string;
        linked: boolean;
        edition: EditionStatusResponse;
        message: string;
        status: Record<string, unknown>;
      }>("POST", "/v1/cloud/unlink", { body: payload });
    },
    getCloudRetention(projectId: string) {
      return request<{
        project_id: string;
        edition_id: string;
        linked: boolean;
        tier: string;
        retention_days: number | null;
        retention_mode?: string;
        indefinite?: boolean;
        support_sla?: string | null;
        configurable?: boolean;
        source?: string;
        local_receipts?: Record<string, unknown>;
        message?: string | null;
        bounds?: Record<string, number>;
        reason?: string | null;
        updated_by?: string | null;
        updated_at?: string | null;
        receipt_id?: string;
        [key: string]: unknown;
      }>("GET", "/v1/cloud/retention", {
        query: { project_id: projectId },
      });
    },
    putCloudRetention(payload: {
      project_id: string;
      retention_days: number;
      reason?: string;
      actor?: string;
    }) {
      return request<{
        project_id: string;
        linked: boolean;
        tier: string;
        retention_days: number | null;
        support_sla?: string | null;
        receipt_id?: string;
        updated?: boolean;
        message?: string | null;
        [key: string]: unknown;
      }>("PUT", "/v1/cloud/retention", { body: payload });
    },
    listEnterpriseIdentityRolloutPolicies(projectId: string) {
      return request<EnterpriseIdentityRolloutPolicyListResponse>(
        "GET",
        "/v1/enterprise/identity-rollout-policies",
        { query: { project_id: projectId } },
      );
    },
    upsertEnterpriseIdentityRolloutPolicy(
      payload: EnterpriseIdentityRolloutPolicyUpsertRequest,
    ) {
      return request<EnterpriseIdentityRolloutPolicySummary>(
        "POST",
        "/v1/enterprise/identity-rollout-policies",
        { body: payload },
      );
    },
    listEnterpriseIdentityProviders(projectId: string) {
      return request<EnterpriseIdentityProviderListResponse>(
        "GET",
        "/v1/enterprise/identity-providers",
        { query: { project_id: projectId } },
      );
    },
    upsertEnterpriseIdentityProvider(
      payload: EnterpriseIdentityProviderUpsertRequest,
    ) {
      return request<EnterpriseIdentityProviderSummary>(
        "POST",
        "/v1/enterprise/identity-providers",
        { body: payload },
      );
    },
    listEnterpriseScimGroups(projectId: string) {
      return request<EnterpriseScimGroupListResponse>(
        "GET",
        "/v1/enterprise/scim/groups",
        { query: { project_id: projectId } },
      );
    },
    upsertEnterpriseScimGroup(payload: EnterpriseScimGroupUpsertRequest) {
      return request<EnterpriseScimGroupSummary>(
        "POST",
        "/v1/enterprise/scim/groups",
        { body: payload },
      );
    },
    listEnterpriseScimUsers(projectId: string) {
      return request<EnterpriseScimUserListResponse>(
        "GET",
        "/v1/enterprise/scim/users",
        { query: { project_id: projectId } },
      );
    },
    upsertEnterpriseScimUser(payload: EnterpriseScimUserUpsertRequest) {
      return request<EnterpriseScimUserSummary>(
        "POST",
        "/v1/enterprise/scim/users",
        { body: payload },
      );
    },
    listProjectMembers(projectId: string) {
      return request<ProjectMemberListResponse>("GET", "/v1/members", {
        query: { project_id: projectId },
      });
    },
    getProjectMember(projectId: string, memberId: string) {
      return request<ProjectMemberSummary>(
        "GET",
        `/v1/members/${encodeURIComponent(memberId)}`,
        { query: { project_id: projectId } },
      );
    },
    upsertProjectMember(payload: ProjectMemberUpsertRequest) {
      return request<ProjectMemberSummary>("POST", "/v1/members", {
        body: payload,
      });
    },
    listOrgMembers(orgId: string, projectId: string, actor = "ceo") {
      return request<OrgMemberListResponse>("GET", "/v1/org-members", {
        query: { org_id: orgId, project_id: projectId, actor },
      });
    },
    upsertOrgMember(payload: OrgMemberUpsertRequest) {
      return request<OrgMemberSummary>("POST", "/v1/org-members", {
        body: payload,
      });
    },
    listBreakGlassGrants(projectId: string, includeExpired = false) {
      return request<BreakGlassGrantListResponse>("GET", "/v1/break-glass/grants", {
        query: { project_id: projectId, include_expired: includeExpired },
      });
    },
    createBreakGlassGrant(payload: BreakGlassGrantCreateRequest) {
      return request<BreakGlassGrantSummary>("POST", "/v1/break-glass/grants", {
        body: payload,
      });
    },
    revokeBreakGlassGrant(
      grantId: string,
      payload: BreakGlassGrantRevokeRequest,
    ) {
      return request<BreakGlassGrantSummary>(
        "POST",
        `/v1/break-glass/grants/${encodeURIComponent(grantId)}/revoke`,
        { body: payload },
      );
    },
    getStatus(projectId: string) {
      return request<StatusResponse>("POST", "/v1/status/load", {
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
    chatWithAgent(payload: AgentChatRequest) {
      return request<AgentChatResponse>("POST", "/v1/engine/agent/chat", {
        body: payload,
      });
    },
    listTasks(projectId: string, limit = 50, offset = 0) {
      return request<TaskListResponse>("GET", "/v1/tasks", {
        query: { project_id: projectId, limit, offset },
      });
    },
    createTask(payload: TaskCreateRequest) {
      return request<TaskSummary>("POST", "/v1/tasks", {
        body: payload,
      });
    },
    listDepartments(projectId: string) {
      return request<DepartmentListResponse>("GET", "/v1/departments", {
        query: { project_id: projectId },
      });
    },
    createDepartment(payload: DepartmentCreateRequest) {
      return request<DepartmentSummary>("POST", "/v1/departments", {
        body: payload,
      });
    },
    listKpis(projectId: string, limit = 50, offset = 0) {
      return request<KpiListResponse>("GET", "/v1/kpis", {
        query: { project_id: projectId, limit, offset },
      });
    },
    createKpi(payload: KpiCreateRequest) {
      return request<KpiSummary>("POST", "/v1/kpis", {
        body: payload,
      });
    },
    listPolicyRecords(projectId: string) {
      return request<PolicyRecordListResponse>("GET", "/v1/policies", {
        query: { project_id: projectId },
      });
    },
    createPolicyRecord(payload: PolicyRecordCreateRequest) {
      return request<PolicyRecordSummary>("POST", "/v1/policies", {
        body: payload,
      });
    },
    listPlans(projectId: string) {
      return request<PlanListResponse>("GET", "/v1/plans", {
        query: { project_id: projectId },
      });
    },
    createPlan(payload: PlanCreateRequest) {
      return request<PlanSummary>("POST", "/v1/plans", {
        body: payload,
      });
    },
    listMeetings(projectId: string) {
      return request<MeetingListResponse>("GET", "/v1/meetings", {
        query: { project_id: projectId },
      });
    },
    createMeeting(payload: MeetingCreateRequest) {
      return request<MeetingSummary>("POST", "/v1/meetings", {
        body: payload,
      });
    },
    runMeeting(meetingId: string, payload: MeetingRunRequest) {
      return request<MeetingSummary>("POST", `/v1/meetings/${meetingId}/run`, {
        body: payload,
      });
    },
    createJob(payload: JobCreateRequest) {
      return request<JobSummary>("POST", "/v1/jobs", {
        body: payload,
      });
    },
    listJobs(
      projectId: string,
      options: {
        status?: string;
        kind?: string;
        task_id?: string;
        include_terminal?: boolean;
        sort?: "status_priority" | "updated" | "created";
        limit?: number;
        offset?: number;
      } = {},
    ) {
      return request<JobListResponse>("GET", "/v1/jobs", {
        query: {
          project_id: projectId,
          status: options.status,
          kind: options.kind,
          task_id: options.task_id,
          include_terminal: options.include_terminal,
          sort: options.sort,
          limit: options.limit,
          offset: options.offset,
        },
      });
    },
    inspectOpsQueue(projectId: string, limit = 25, includeTerminal = false) {
      return request<OpsQueueInspectionResponse>("GET", "/v1/ops/queue", {
        query: {
          project_id: projectId,
          limit,
          include_terminal: includeTerminal,
        },
      });
    },
    createOpsDigest(payload: OpsDigestRequest) {
      return request<OpsDigestResponse>("POST", "/v1/ops/digests", {
        body: payload,
      });
    },
    getJob(jobId: string) {
      return request<JobSummary>("GET", `/v1/jobs/${encodeURIComponent(jobId)}`);
    },
    getTask(taskId: string) {
      return request<TaskSummary>("GET", `/v1/tasks/${encodeURIComponent(taskId)}`);
    },
    listSelfImprovementProposals(projectId: string, limit = 25, offset = 0) {
      return request<SelfImprovementProposalListResponse>(
        "GET",
        "/v1/self-improvement/proposals",
        {
          query: { project_id: projectId, limit, offset },
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
    listArtifacts(projectId: string, limit = 25, offset = 0) {
      return request<ArtifactListResponse>("GET", "/v1/artifacts", {
        query: { project_id: projectId, limit, offset },
      });
    },
    getArtifact(artifactId: string) {
      return request<ArtifactDetail>(
        "GET",
        `/v1/artifacts/${encodeURIComponent(artifactId)}`,
      );
    },
    listChangeJournal(projectId: string, limit = 25, offset = 0) {
      return request<ChangeJournalResponse>("GET", "/v1/change-journal", {
        query: { project_id: projectId, limit, offset },
      });
    },
    getChangeJournalItem(projectId: string, artifactId: string) {
      return request<ChangeJournalDetail>(
        "GET",
        `/v1/change-journal/${encodeURIComponent(artifactId)}`,
        { query: { project_id: projectId } },
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
    checkRoleAutonomy(roleKey: string, payload: RoleAutonomyCheckRequest) {
      return request<RoleAutonomyCheckResponse>(
        "POST",
        `/v1/roles/${encodeURIComponent(roleKey)}/autonomy/check`,
        { body: payload },
      );
    },
    checkRoleScope(roleKey: string, payload: RoleScopeCheckRequest) {
      return request<RoleScopeCheckResponse>(
        "POST",
        `/v1/roles/${encodeURIComponent(roleKey)}/scope/check`,
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
    importPack(payload: PackImportRequest) {
      return request<PackDetail>("POST", "/v1/packs/import", {
        body: payload,
      });
    },
    exportPack(projectId: string, packId: string) {
      return request<string>("GET", `/v1/packs/${encodeURIComponent(packId)}/export`, {
        query: { project_id: projectId },
      });
    },
    listSecretRefs(
      projectId: string,
      options: { provider?: string; status?: string; limit?: number; offset?: number } = {},
    ) {
      return request<SecretRefListResponse>("GET", "/v1/secret-refs", {
        query: {
          project_id: projectId,
          provider: options.provider,
          status: options.status,
          limit: options.limit,
          offset: options.offset,
        },
      });
    },
    createSecretRef(payload: SecretRefCreateRequest) {
      return request<SecretRefSummary>("POST", "/v1/secret-refs", {
        body: payload,
      });
    },
    getSecretRef(projectId: string, secretRefId: string) {
      return request<SecretRefSummary>(
        "GET",
        `/v1/secret-refs/${encodeURIComponent(secretRefId)}`,
        { query: { project_id: projectId } },
      );
    },
    updateSecretRef(secretRefId: string, payload: SecretRefUpdateRequest) {
      return request<SecretRefSummary>(
        "PATCH",
        `/v1/secret-refs/${encodeURIComponent(secretRefId)}`,
        { body: payload },
      );
    },
    deleteSecretRef(projectId: string, secretRefId: string, actor = "system") {
      return request<void>(
        "DELETE",
        `/v1/secret-refs/${encodeURIComponent(secretRefId)}`,
        { query: { project_id: projectId, actor } },
      );
    },
    listAgentModules(projectId: string) {
      return request<AgentModuleListResponse>("GET", "/v1/agent-modules", {
        query: { project_id: projectId },
      });
    },
    getAgentModule(projectId: string, moduleId: string) {
      return request<AgentModuleDetail>(
        "GET",
        `/v1/agent-modules/${encodeURIComponent(moduleId)}`,
        {
          query: { project_id: projectId },
        },
      );
    },
    installAgentModule(payload: AgentModuleInstallRequest) {
      return request<AgentModuleDetail>("POST", "/v1/agent-modules/install", {
        body: payload,
      });
    },
    uninstallAgentModule(
      moduleId: string,
      payload: AgentModuleLifecycleRequest,
    ) {
      return request<AgentModuleDetail>(
        "POST",
        `/v1/agent-modules/${encodeURIComponent(moduleId)}/uninstall`,
        { body: payload },
      );
    },
    rollbackAgentModule(
      moduleId: string,
      payload: AgentModuleLifecycleRequest,
    ) {
      return request<AgentModuleDetail>(
        "POST",
        `/v1/agent-modules/${encodeURIComponent(moduleId)}/rollback`,
        { body: payload },
      );
    },
    resolveAgentRuntime(payload: AgentRuntimeResolveRequest) {
      return request<AgentRuntimeResolveResponse>(
        "POST",
        "/v1/agent-runtime/resolve",
        { body: payload },
      );
    },
    listAgentModuleInstallRecords(
      projectId: string,
      moduleId?: string,
      limit = 50,
    ) {
      return request<AgentModuleInstallRecordListResponse>(
        "GET",
        "/v1/agent-module-install-records",
        {
          query: {
            project_id: projectId,
            ...(moduleId ? { module_id: moduleId } : {}),
            limit: String(limit),
          },
        },
      );
    },
    listSkills(projectId?: string) {
      // SCOPED TO THE PROJECT, OR THE PICKER OFFERS WHAT PUBLISHING WILL REFUSE.
      //
      // Unscoped this returns every skill file on disk — 71 of them. The entitlement broker
      // filters by the project's edition bundles, so a project is entitled to 23, and that is
      // the set Agent Studio validates a new agent against. Ticking any of the other 48 in the
      // wizard produced "Requested skills are not installed" at publish, on a skill the UI had
      // just offered.
      const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
      return request<SkillListResponse>("GET", `/v1/skills${query}`);
    },
    importSkill(payload: SkillImportRequest) {
      return request<SkillImportResponse>("POST", "/v1/skills/import", {
        body: payload,
      });
    },
    exportSkill(projectId: string, skillId: string, actor = "ceo") {
      return request<string>(
        "GET",
        `/v1/skills/${encodeURIComponent(skillId)}/export`,
        { query: { project_id: projectId, actor } },
      );
    },
    listModelAdapters() {
      return request<ModelAdapterListResponse>("GET", "/v1/model-adapters");
    },
    listEntitlementPools(projectId: string) {
      return request<EntitlementPoolListResponse>("GET", "/v1/entitlement-pools", {
        query: { project_id: projectId },
      });
    },
    probeEntitlementPool(
      poolId: string,
      payload: EntitlementPoolProbeRequest,
    ) {
      return request<EntitlementPoolProbeResponse>(
        "POST",
        `/v1/entitlement-pools/${encodeURIComponent(poolId)}/probe`,
        {
          body: payload,
        },
      );
    },
    listModelRouteDecisions(
      projectId: string,
      filtersOrLimit: number | ModelRouteDecisionListFilters = 25,
    ) {
      const filters =
        typeof filtersOrLimit === "number"
          ? { limit: filtersOrLimit }
          : filtersOrLimit;
      return request<ModelRouteDecisionListResponse>(
        "GET",
        "/v1/model-route-decisions",
        {
          query: {
            project_id: projectId,
            limit: filters.limit ?? 25,
            skill_id: filters.skill_id,
            task_kind: filters.task_kind,
            workload_class: filters.workload_class,
            provider: filters.provider,
            source_id: filters.source_id,
            model_id: filters.model_id,
            routing_mode: filters.routing_mode,
            budget_state: filters.budget_state,
            data_boundary: filters.data_boundary,
            risk_tier: filters.risk_tier,
            denial_reason: filters.denial_reason,
            denied_only: filters.denied_only,
            selected_only: filters.selected_only,
          },
        },
      );
    },
    simulateModelRouteDecision(payload: ModelRouteDecisionSimulateRequest) {
      return request<ModelRouteDecisionSimulateResponse>(
        "POST",
        "/v1/model-route-decisions/simulate",
        {
          body: payload,
        },
      );
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
    connectEntitlementSource(
      sourceId: string,
      payload: EntitlementSourceConnectRequest,
    ) {
      return request<EntitlementSourceSummary>(
        "POST",
        `/v1/entitlements/sources/${encodeURIComponent(sourceId)}/connect`,
        {
          body: payload,
        },
      );
    },
    uploadModelCredential(
      sourceId: string,
      payload: ModelCredentialUploadRequest,
    ) {
      return request<ModelCredentialWriteSummary>(
        "POST",
        `/v1/entitlements/sources/${encodeURIComponent(sourceId)}/credential`,
        { body: payload },
      );
    },
    listEntitlementConnectionSessions(
      projectId: string,
      options: { source_id?: string; limit?: number } = {},
    ) {
      return request<EntitlementConnectionSessionListResponse>(
        "GET",
        "/v1/entitlement-connection-sessions",
        {
          query: {
            project_id: projectId,
            source_id: options.source_id,
            limit: options.limit ?? 25,
          },
        },
      );
    },
    createEntitlementConnectionSession(
      sourceId: string,
      payload: EntitlementConnectionSessionCreateRequest,
    ) {
      return request<EntitlementConnectionSessionSummary>(
        "POST",
        `/v1/entitlements/sources/${encodeURIComponent(sourceId)}/connection-sessions`,
        {
          body: payload,
        },
      );
    },
    completeEntitlementConnectionSession(
      sessionId: string,
      payload: EntitlementConnectionSessionCompleteRequest,
    ) {
      return request<EntitlementConnectionSessionCompleteResponse>(
        "POST",
        `/v1/entitlement-connection-sessions/${encodeURIComponent(sessionId)}/complete`,
        {
          body: payload,
        },
      );
    },
    disconnectEntitlementSource(
      sourceId: string,
      payload: EntitlementSourceDisconnectRequest,
    ) {
      return request<EntitlementSourceSummary>(
        "POST",
        `/v1/entitlements/sources/${encodeURIComponent(sourceId)}/disconnect`,
        {
          body: payload,
        },
      );
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
    listProviderAccounts(projectId: string) {
      return request<ProviderAccountListResponse>("GET", "/v1/providers", {
        query: { project_id: projectId },
      });
    },
    listGitHubConnections(projectId: string) {
      return request<GitHubConnectionListResponse>(
        "GET",
        "/v1/github/connections",
        { query: { project_id: projectId } },
      );
    },
    createGitHubConnection(payload: GitHubConnectionCreateRequest) {
      return request<GitHubConnectionSummary>(
        "POST",
        "/v1/github/connections",
        { body: payload },
      );
    },
    easyConnectGitHub(payload: GitHubEasyConnectRequest) {
      return request<GitHubEasyConnectResponse>(
        "POST",
        "/v1/github/easy-connect",
        { body: payload },
      );
    },
    listGitHubAccessibleRepositories(
      projectId: string,
      connectionId: string,
      limit = 50,
    ) {
      return request<GitHubAccessibleRepositoryListResponse>(
        "GET",
        `/v1/github/connections/${encodeURIComponent(connectionId)}/repositories`,
        { query: { project_id: projectId, limit } },
      );
    },
    getGitHubConnection(projectId: string, connectionId: string) {
      return request<GitHubConnectionSummary>(
        "GET",
        `/v1/github/connections/${encodeURIComponent(connectionId)}`,
        { query: { project_id: projectId } },
      );
    },
    enableGitHubConnection(
      connectionId: string,
      payload: GitHubConnectionLifecycleRequest,
    ) {
      return request<GitHubConnectionSummary>(
        "POST",
        `/v1/github/connections/${encodeURIComponent(connectionId)}/enable`,
        { body: payload },
      );
    },
    disableGitHubConnection(
      connectionId: string,
      payload: GitHubConnectionLifecycleRequest,
    ) {
      return request<GitHubConnectionSummary>(
        "POST",
        `/v1/github/connections/${encodeURIComponent(connectionId)}/disable`,
        { body: payload },
      );
    },
    revokeGitHubConnection(
      connectionId: string,
      payload: GitHubConnectionLifecycleRequest,
    ) {
      return request<GitHubConnectionSummary>(
        "POST",
        `/v1/github/connections/${encodeURIComponent(connectionId)}/revoke`,
        { body: payload },
      );
    },
    testGitHubConnection(
      connectionId: string,
      payload: GitHubConnectionTestRequest,
    ) {
      return request<GitHubConnectionTestResponse>(
        "POST",
        `/v1/github/connections/${encodeURIComponent(connectionId)}/test`,
        { body: payload },
      );
    },
    listGitHubRepositoryBindings(projectId: string, connectionId?: string) {
      return request<GitHubRepositoryBindingListResponse>(
        "GET",
        "/v1/github/repository-bindings",
        {
          query: {
            project_id: projectId,
            connection_id: connectionId,
          },
        },
      );
    },
    createGitHubRepositoryBinding(
      payload: GitHubRepositoryBindingCreateRequest,
    ) {
      return request<GitHubRepositoryBindingSummary>(
        "POST",
        "/v1/github/repository-bindings",
        { body: payload },
      );
    },
    getGitHubRepositoryBinding(projectId: string, bindingId: string) {
      return request<GitHubRepositoryBindingSummary>(
        "GET",
        `/v1/github/repository-bindings/${encodeURIComponent(bindingId)}`,
        { query: { project_id: projectId } },
      );
    },
    updateGitHubRepositoryBinding(
      bindingId: string,
      payload: GitHubRepositoryBindingUpdateRequest,
    ) {
      return request<GitHubRepositoryBindingSummary>(
        "PATCH",
        `/v1/github/repository-bindings/${encodeURIComponent(bindingId)}`,
        { body: payload },
      );
    },
    listGitHubBranchPolicies(
      projectId: string,
      options: {
        repository_binding_id?: string;
        include_history?: boolean;
      } = {},
    ) {
      return request<GitHubBranchPolicyListResponse>(
        "GET",
        "/v1/github/branch-policies",
        {
          query: {
            project_id: projectId,
            repository_binding_id: options.repository_binding_id,
            include_history: options.include_history,
          },
        },
      );
    },
    createGitHubBranchPolicy(payload: GitHubBranchPolicyCreateRequest) {
      return request<GitHubBranchPolicySummary>(
        "POST",
        "/v1/github/branch-policies",
        { body: payload },
      );
    },
    getGitHubBranchPolicy(projectId: string, policyId: string) {
      return request<GitHubBranchPolicySummary>(
        "GET",
        `/v1/github/branch-policies/${encodeURIComponent(policyId)}`,
        { query: { project_id: projectId } },
      );
    },
    reviseGitHubBranchPolicy(
      policyId: string,
      payload: GitHubBranchPolicyUpdateRequest,
    ) {
      return request<GitHubBranchPolicySummary>(
        "PATCH",
        `/v1/github/branch-policies/${encodeURIComponent(policyId)}`,
        { body: payload },
      );
    },
    evaluateGitHubBranchPolicy(
      policyId: string,
      payload: GitHubBranchPolicyEvaluateRequest,
    ) {
      return request<GitHubBranchPolicyEvaluationResponse>(
        "POST",
        `/v1/github/branch-policies/${encodeURIComponent(policyId)}/evaluate`,
        { body: payload },
      );
    },
    listGitHubBranchSnapshots(
      projectId: string,
      options: { repository_binding_id?: string; limit?: number } = {},
    ) {
      return request<GitHubBranchSnapshotListResponse>(
        "GET",
        "/v1/github/branch-snapshots",
        {
          query: {
            project_id: projectId,
            repository_binding_id: options.repository_binding_id,
            limit: options.limit ?? 50,
          },
        },
      );
    },
    captureGitHubBranchSnapshot(payload: GitHubBranchSnapshotCaptureRequest) {
      return request<GitHubBranchSnapshotCaptureResponse>(
        "POST",
        "/v1/github/branch-snapshots/capture",
        { body: payload },
      );
    },
    getGitHubBranchSnapshot(projectId: string, snapshotId: string) {
      return request<GitHubBranchSnapshotSummary>(
        "GET",
        `/v1/github/branch-snapshots/${encodeURIComponent(snapshotId)}`,
        { query: { project_id: projectId } },
      );
    },
    listGitHubBranchLeases(
      projectId: string,
      options: {
        repository_binding_id?: string;
        ref_name?: string;
        include_history?: boolean;
        limit?: number;
      } = {},
    ) {
      return request<GitHubBranchLeaseListResponse>(
        "GET",
        "/v1/github/branch-leases",
        {
          query: {
            project_id: projectId,
            repository_binding_id: options.repository_binding_id,
            ref_name: options.ref_name,
            include_history: options.include_history,
            limit: options.limit ?? 100,
          },
        },
      );
    },
    acquireGitHubBranchLease(payload: GitHubBranchLeaseAcquireRequest) {
      return request<GitHubBranchLeaseAcquireResponse>(
        "POST",
        "/v1/github/branch-leases",
        { body: payload },
      );
    },
    getGitHubBranchLease(projectId: string, leaseId: string) {
      return request<GitHubBranchLeaseSummary>(
        "GET",
        `/v1/github/branch-leases/${encodeURIComponent(leaseId)}`,
        { query: { project_id: projectId } },
      );
    },
    heartbeatGitHubBranchLease(
      leaseId: string,
      payload: GitHubBranchLeaseHeartbeatRequest,
    ) {
      return request<GitHubBranchLeaseSummary>(
        "POST",
        `/v1/github/branch-leases/${encodeURIComponent(leaseId)}/heartbeat`,
        { body: payload },
      );
    },
    releaseGitHubBranchLease(
      leaseId: string,
      payload: GitHubBranchLeaseReleaseRequest,
    ) {
      return request<GitHubBranchLeaseSummary>(
        "POST",
        `/v1/github/branch-leases/${encodeURIComponent(leaseId)}/release`,
        { body: payload },
      );
    },
    reclaimGitHubBranchLease(
      leaseId: string,
      payload: GitHubBranchLeaseReclaimRequest,
    ) {
      return request<GitHubBranchLeaseSummary>(
        "POST",
        `/v1/github/branch-leases/${encodeURIComponent(leaseId)}/reclaim`,
        { body: payload },
      );
    },
    listGitHubCodeSyncRuns(
      projectId: string,
      options: {
        repository_binding_id?: string;
        status?: string;
        limit?: number;
      } = {},
    ) {
      return request<GitHubCodeSyncRunListResponse>(
        "GET",
        "/v1/github/code-sync-runs",
        {
          query: {
            project_id: projectId,
            repository_binding_id: options.repository_binding_id,
            status: options.status,
            limit: options.limit ?? 100,
          },
        },
      );
    },
    planGitHubCodeSync(payload: GitHubCodeSyncPlanRequest) {
      return request<GitHubCodeSyncRunSummary>(
        "POST",
        "/v1/github/code-sync-runs/plan",
        { body: payload },
      );
    },
    getGitHubCodeSyncRun(projectId: string, syncRunId: string) {
      return request<GitHubCodeSyncRunSummary>(
        "GET",
        `/v1/github/code-sync-runs/${encodeURIComponent(syncRunId)}`,
        { query: { project_id: projectId } },
      );
    },
    applyGitHubCodeSync(
      syncRunId: string,
      payload: GitHubCodeSyncApplyRequest,
    ) {
      return request<GitHubCodeSyncRunSummary>(
        "POST",
        `/v1/github/code-sync-runs/${encodeURIComponent(syncRunId)}/apply`,
        { body: payload },
      );
    },
    listGitHubConflictSets(
      projectId: string,
      options: { status?: string; limit?: number } = {},
    ) {
      return request<GitHubConflictSetListResponse>(
        "GET",
        "/v1/github/conflict-sets",
        {
          query: {
            project_id: projectId,
            status: options.status,
            limit: options.limit ?? 100,
          },
        },
      );
    },
    getGitHubConflictSet(projectId: string, conflictSetId: string) {
      return request<GitHubConflictSetSummary>(
        "GET",
        `/v1/github/conflict-sets/${encodeURIComponent(conflictSetId)}`,
        { query: { project_id: projectId } },
      );
    },
    listGitHubResolutionPlans(
      projectId: string,
      options: { conflict_set_id?: string; limit?: number } = {},
    ) {
      return request<GitHubResolutionPlanListResponse>(
        "GET",
        "/v1/github/resolution-plans",
        {
          query: {
            project_id: projectId,
            conflict_set_id: options.conflict_set_id,
            limit: options.limit ?? 100,
          },
        },
      );
    },
    createGitHubResolutionPlan(
      conflictSetId: string,
      payload: GitHubResolutionPlanCreateRequest,
    ) {
      return request<GitHubResolutionPlanSummary>(
        "POST",
        `/v1/github/conflict-sets/${encodeURIComponent(conflictSetId)}/resolution-plans`,
        { body: payload },
      );
    },
    getGitHubResolutionPlan(projectId: string, planId: string) {
      return request<GitHubResolutionPlanSummary>(
        "GET",
        `/v1/github/resolution-plans/${encodeURIComponent(planId)}`,
        { query: { project_id: projectId } },
      );
    },
    approveGitHubResolutionPlan(
      planId: string,
      payload: GitHubResolutionPlanApproveRequest,
    ) {
      return request<GitHubResolutionPlanSummary>(
        "POST",
        `/v1/github/resolution-plans/${encodeURIComponent(planId)}/approve`,
        { body: payload },
      );
    },
    applyGitHubResolutionPlan(
      planId: string,
      payload: GitHubResolutionPlanApplyRequest,
    ) {
      return request<GitHubResolutionApplyResponse>(
        "POST",
        `/v1/github/resolution-plans/${encodeURIComponent(planId)}/apply`,
        { body: payload },
      );
    },
    listGitHubMergeAuthorizations(
      projectId: string,
      options: {
        repository_binding_id?: string;
        target_ref?: string;
        status?: string;
        limit?: number;
      } = {},
    ) {
      return request<GitHubMergeAuthorizationListResponse>(
        "GET",
        "/v1/github/merge-authorizations",
        {
          query: {
            project_id: projectId,
            repository_binding_id: options.repository_binding_id,
            target_ref: options.target_ref,
            status: options.status,
            limit: options.limit ?? 100,
          },
        },
      );
    },
    createGitHubMergeAuthorization(
      payload: GitHubMergeAuthorizationCreateRequest,
    ) {
      return request<GitHubMergeAuthorizationSummary>(
        "POST",
        "/v1/github/merge-authorizations",
        { body: payload },
      );
    },
    getGitHubMergeAuthorization(
      projectId: string,
      authorizationId: string,
    ) {
      return request<GitHubMergeAuthorizationSummary>(
        "GET",
        `/v1/github/merge-authorizations/${encodeURIComponent(authorizationId)}`,
        { query: { project_id: projectId } },
      );
    },
    authorizeGitHubMerge(
      authorizationId: string,
      payload: GitHubMergeAuthorizationApproveRequest,
    ) {
      return request<GitHubMergeAuthorizationSummary>(
        "POST",
        `/v1/github/merge-authorizations/${encodeURIComponent(authorizationId)}/authorize`,
        { body: payload },
      );
    },
    enqueueGitHubMerge(
      authorizationId: string,
      payload: GitHubMergeAuthorizationEnqueueRequest,
    ) {
      return request<GitHubMergeAuthorizationSummary>(
        "POST",
        `/v1/github/merge-authorizations/${encodeURIComponent(authorizationId)}/enqueue`,
        { body: payload },
      );
    },
    applyGitHubMerge(
      authorizationId: string,
      payload: GitHubMergeAuthorizationApplyRequest,
    ) {
      return request<GitHubMergeAuthorizationSummary>(
        "POST",
        `/v1/github/merge-authorizations/${encodeURIComponent(authorizationId)}/apply`,
        { body: payload },
      );
    },
    listGitHubReconciliationRuns(
      projectId: string,
      options: {
        repository_binding_id?: string;
        status?: string;
        limit?: number;
      } = {},
    ) {
      return request<GitHubReconciliationRunListResponse>(
        "GET",
        "/v1/github/reconciliation-runs",
        {
          query: {
            project_id: projectId,
            repository_binding_id: options.repository_binding_id,
            status: options.status,
            limit: options.limit ?? 100,
          },
        },
      );
    },
    scheduleGitHubReconciliationPoll(
      payload: GitHubReconciliationPollRequest,
    ) {
      return request<GitHubReconciliationPollResponse>(
        "POST",
        "/v1/github/reconciliation-runs/poll",
        { body: payload },
      );
    },
    getGitHubReconciliationRun(projectId: string, runId: string) {
      return request<GitHubReconciliationRunSummary>(
        "GET",
        `/v1/github/reconciliation-runs/${encodeURIComponent(runId)}`,
        { query: { project_id: projectId } },
      );
    },
    executeGitHubReconciliationRun(
      runId: string,
      payload: GitHubReconciliationExecuteRequest,
    ) {
      return request<GitHubReconciliationExecuteResponse>(
        "POST",
        `/v1/github/reconciliation-runs/${encodeURIComponent(runId)}/execute`,
        { body: payload },
      );
    },
    repairGitHubReconciliationRun(
      runId: string,
      payload: GitHubReconciliationRepairRequest,
    ) {
      return request<GitHubReconciliationRunSummary>(
        "POST",
        `/v1/github/reconciliation-runs/${encodeURIComponent(runId)}/repair`,
        { body: payload },
      );
    },
    listGitHubReconciliationSignals(
      projectId: string,
      options: {
        repository_binding_id?: string;
        status?: string;
        limit?: number;
      } = {},
    ) {
      return request<GitHubReconciliationSignalListResponse>(
        "GET",
        "/v1/github/reconciliation-signals",
        {
          query: {
            project_id: projectId,
            repository_binding_id: options.repository_binding_id,
            status: options.status,
            limit: options.limit ?? 100,
          },
        },
      );
    },
    listGitHubRepositoryRefStates(
      projectId: string,
      options: { repository_binding_id?: string; limit?: number } = {},
    ) {
      return request<GitHubRepositoryRefStateListResponse>(
        "GET",
        "/v1/github/repository-ref-states",
        {
          query: {
            project_id: projectId,
            repository_binding_id: options.repository_binding_id,
            limit: options.limit ?? 200,
          },
        },
      );
    },
    listProviderDefinitions() {
      return request<ProviderDefinitionListResponse>("GET", "/v1/provider-definitions");
    },
    createProviderAccount(payload: ProviderAccountCreateRequest) {
      return request<ProviderAccountSummary>("POST", "/v1/providers", {
        body: payload,
      });
    },
    getProviderAccount(providerAccountId: string, projectId: string) {
      return request<ProviderAccountSummary>(
        "GET",
        `/v1/providers/${encodeURIComponent(providerAccountId)}`,
        { query: { project_id: projectId } },
      );
    },
    updateProviderAccount(
      providerAccountId: string,
      payload: ProviderAccountUpdateRequest,
    ) {
      return request<ProviderAccountSummary>(
        "PATCH",
        `/v1/providers/${encodeURIComponent(providerAccountId)}`,
        { body: payload },
      );
    },
    enableProviderAccount(
      providerAccountId: string,
      payload: ProviderAccountLifecycleRequest,
    ) {
      return request<ProviderAccountSummary>(
        "POST",
        `/v1/providers/${encodeURIComponent(providerAccountId)}/enable`,
        { body: payload },
      );
    },
    disableProviderAccount(
      providerAccountId: string,
      payload: ProviderAccountLifecycleRequest,
    ) {
      return request<ProviderAccountSummary>(
        "POST",
        `/v1/providers/${encodeURIComponent(providerAccountId)}/disable`,
        { body: payload },
      );
    },
    revokeProviderAccount(
      providerAccountId: string,
      payload: ProviderAccountLifecycleRequest,
    ) {
      return request<ProviderAccountSummary>(
        "POST",
        `/v1/providers/${encodeURIComponent(providerAccountId)}/revoke`,
        { body: payload },
      );
    },
    rotateProviderCredentials(
      providerAccountId: string,
      payload: ProviderCredentialRotateRequest,
    ) {
      return request<ProviderCredentialRotationResponse>(
        "POST",
        `/v1/providers/${encodeURIComponent(providerAccountId)}/rotate`,
        { body: payload },
      );
    },
    testProviderAccount(providerAccountId: string, projectId: string, actor = "ceo") {
      return request<ProviderConnectionTestResponse>(
        "POST",
        `/v1/providers/${encodeURIComponent(providerAccountId)}/test`,
        { query: { project_id: projectId, actor } },
      );
    },
    listExternalResources(
      projectId: string,
      options?: { provider?: string; ownership?: string; limit?: number; offset?: number },
    ) {
      return request<ExternalResourceListResponse>("GET", "/v1/resources", {
        query: {
          project_id: projectId,
          provider: options?.provider,
          ownership: options?.ownership,
          limit: options?.limit,
          offset: options?.offset,
        },
      });
    },
    syncExternalResources(payload: ResourceSyncRequest) {
      return request<ResourceSyncResponse>("POST", "/v1/resources/sync", {
        body: payload,
      });
    },
    listProviderCosts(projectId: string, limit = 100, offset = 0) {
      return request<CostEventListResponse>("GET", "/v1/costs", {
        query: { project_id: projectId, limit, offset },
      });
    },
    listProviderCostAnomalies(projectId: string, limit = 100, offset = 0) {
      return request<CostAnomalyListResponse>("GET", "/v1/cost-anomalies", {
        query: { project_id: projectId, limit, offset },
      });
    },
    listChangeSets(projectId: string, status?: string, limit = 100, offset = 0) {
      return request<ChangeSetListResponse>("GET", "/v1/changesets", {
        query: { project_id: projectId, status, limit, offset },
      });
    },
    createChangeSet(payload: ChangeSetCreateRequest) {
      return request<ChangeSetSummary>("POST", "/v1/changesets", {
        body: payload,
      });
    },
    approveChangeSet(changesetId: string, payload: ChangeSetApproveRequest = {}) {
      return request<ChangeSetSummary>(
        "POST",
        `/v1/changesets/${encodeURIComponent(changesetId)}/approve`,
        { body: payload },
      );
    },
    applyChangeSet(changesetId: string, payload: ChangeSetApplyRequest = {}) {
      return request<ChangeSetApplyResponse>(
        "POST",
        `/v1/changesets/${encodeURIComponent(changesetId)}/apply`,
        { body: payload },
      );
    },
    listResourceReceipts(projectId: string, limit = 100, offset = 0) {
      return request<ResourceReceiptListResponse>("GET", "/v1/resource-receipts", {
        query: { project_id: projectId, limit, offset },
      });
    },
    listWorkerPools(projectId: string) {
      return request<WorkerPoolListResponse>("GET", "/v1/worker-pools", {
        query: { project_id: projectId },
      });
    },
    upsertWorkerPool(payload: WorkerPoolUpsertRequest) {
      return request<WorkerPoolSummary>("POST", "/v1/worker-pools", {
        body: payload,
      });
    },
    getElasticStatus(projectId: string) {
      return request<Record<string, unknown>>("POST", "/v1/elastic/status/load", {
        query: { project_id: projectId },
      });
    },
    requestElasticCapacity(payload: {
      project_id: string;
      desired_workers: number;
      actor?: string;
      pool_key?: string;
      worker_pool_id?: string;
      kind?: "local" | "container" | "cloud" | "vm";
      provider_account_id?: string;
      auto_reconcile?: boolean;
      cloud_spillover_enabled?: boolean;
      reason?: string;
    }) {
      return request<Record<string, unknown>>("POST", "/v1/elastic/capacity", {
        body: payload,
      });
    },
    reconcileElasticPool(
      workerPoolId: string,
      payload: {
        project_id: string;
        actor?: string;
        cloud_spillover_enabled?: boolean;
        reason?: string;
      },
    ) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/elastic/pools/${encodeURIComponent(workerPoolId)}/reconcile`,
        { body: payload },
      );
    },
    listElasticInstances(
      projectId: string,
      query: {
        worker_pool_id?: string;
        status?: string;
        active_only?: boolean;
        limit?: number;
      } = {},
    ) {
      return request<{
        project_id: string;
        items: Array<Record<string, unknown>>;
        count: number;
      }>("POST", "/v1/elastic/instances/load", {
        query: { project_id: projectId, ...query },
      });
    },
    heartbeatElasticInstance(
      instanceId: string,
      payload: {
        project_id: string;
        actor?: string;
        status?: string;
        endpoint?: string;
        metadata?: Record<string, unknown>;
      },
    ) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/elastic/instances/${encodeURIComponent(instanceId)}/heartbeat`,
        { body: payload },
      );
    },
    terminateElasticInstance(
      instanceId: string,
      payload: {
        project_id: string;
        actor?: string;
        reason?: string;
        destroy_provider_resource?: boolean;
      },
    ) {
      return request<Record<string, unknown>>(
        "POST",
        `/v1/elastic/instances/${encodeURIComponent(instanceId)}/terminate`,
        { body: payload },
      );
    },
    listProviderUsage(
      projectId: string,
      query: {
        provider?: string;
        provider_account_id?: string;
        category?: string;
        limit?: number;
      } = {},
    ) {
      return request<{
        project_id: string;
        items: Array<Record<string, unknown>>;
        count: number;
      }>("POST", "/v1/provider-usage/load", {
        query: { project_id: projectId, ...query },
      });
    },
    getProviderUsageSummary(projectId: string, provider?: string) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/provider-usage/summary/load",
        { query: { project_id: projectId, provider } },
      );
    },
    ingestProviderUsage(payload: {
      project_id: string;
      provider: string;
      operation: string;
      actor?: string;
      category?: string;
      quantity?: number;
      unit?: string;
      cost_usd_micros?: number;
      provider_account_id?: string;
      resource_ref?: string;
      metadata?: Record<string, unknown>;
    }) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/provider-usage/ingest",
        { body: payload },
      );
    },
    syncProviderUsage(payload: {
      project_id: string;
      provider_account_id: string;
      actor?: string;
    }) {
      return request<Record<string, unknown>>(
        "POST",
        "/v1/provider-usage/sync",
        { body: payload },
      );
    },
    listScalePlans(projectId: string, limit = 50) {
      return request<ScalePlanListResponse>("GET", "/v1/autoscaler/plans", {
        query: { project_id: projectId, limit },
      });
    },
    runAutoscalerPlan(payload: ScalePlanRunRequest) {
      return request<ScalePlanRunResponse>("POST", "/v1/autoscaler/plans", {
        body: payload,
      });
    },
    runResourceJanitor(payload: ResourceJanitorRunRequest) {
      return request<ResourceJanitorRunResponse>(
        "POST",
        "/v1/resource-janitor/run",
        { body: payload },
      );
    },
    listDeploymentTargets(projectId: string, limit = 50, offset = 0) {
      return request<DeploymentTargetListResponse>("GET", "/v1/deployments", {
        query: { project_id: projectId, limit, offset },
      });
    },
    createDeploymentTarget(payload: DeploymentTargetCreateRequest) {
      return request<DeploymentTargetSummary>("POST", "/v1/deployments", {
        body: payload,
      });
    },
    listDeploymentEnvironmentPolicies(projectId: string, limit = 50, offset = 0) {
      return request<DeploymentEnvironmentPolicyListResponse>(
        "GET",
        "/v1/deployment-environment-policies",
        {
          query: { project_id: projectId, limit, offset },
        },
      );
    },
    upsertDeploymentEnvironmentPolicy(
      payload: DeploymentEnvironmentPolicyUpsertRequest,
    ) {
      return request<DeploymentEnvironmentPolicySummary>(
        "POST",
        "/v1/deployment-environment-policies",
        {
          body: payload,
        },
      );
    },
    listDeploymentRuns(projectId: string, limit = 50, offset = 0) {
      return request<DeploymentRunListResponse>("GET", "/v1/deployment-runs", {
        query: { project_id: projectId, limit, offset },
      });
    },
    createDeploymentRun(payload: DeploymentRunCreateRequest) {
      return request<DeploymentRunSummary>("POST", "/v1/deployment-runs", {
        body: payload,
      });
    },
    startDeploymentRollout(
      deploymentRunId: string,
      payload: DeploymentRolloutStartRequest = {},
    ) {
      return request<DeploymentRolloutSummary>(
        "POST",
        `/v1/deployment-runs/${encodeURIComponent(deploymentRunId)}/rollout/start`,
        { body: payload },
      );
    },
    advanceDeploymentRollout(
      deploymentRunId: string,
      payload: DeploymentRolloutAdvanceRequest,
    ) {
      return request<DeploymentRolloutSummary>(
        "POST",
        `/v1/deployment-runs/${encodeURIComponent(deploymentRunId)}/rollout/advance`,
        { body: payload },
      );
    },
    rollbackDeploymentRollout(
      deploymentRunId: string,
      payload: DeploymentRolloutRollbackRequest,
    ) {
      return request<DeploymentRolloutSummary>(
        "POST",
        `/v1/deployment-runs/${encodeURIComponent(deploymentRunId)}/rollout/rollback`,
        { body: payload },
      );
    },
    listDeploymentPromotions(projectId: string, limit = 50) {
      return request<DeploymentPromotionListResponse>(
        "GET",
        "/v1/deployment-promotions",
        { query: { project_id: projectId, limit } },
      );
    },
    getDeploymentPromotion(promotionId: string) {
      return request<DeploymentPromotionSummary>(
        "GET",
        `/v1/deployment-promotions/${encodeURIComponent(promotionId)}`,
      );
    },
    createDeploymentPromotion(payload: DeploymentPromotionStartRequest) {
      return request<DeploymentPromotionSummary>(
        "POST",
        "/v1/deployment-promotions",
        { body: payload },
      );
    },
    advanceDeploymentPromotion(
      promotionId: string,
      payload: DeploymentPromotionAdvanceRequest = {},
    ) {
      return request<DeploymentPromotionSummary>(
        "POST",
        `/v1/deployment-promotions/${encodeURIComponent(promotionId)}/advance`,
        { body: payload },
      );
    },
    runDeploymentHealthCheck(
      deploymentRunId: string,
      payload: DeploymentRunHealthCheckRequest = {},
    ) {
      return request<DeploymentRunHealthCheckResponse>(
        "POST",
        `/v1/deployment-runs/${encodeURIComponent(
          deploymentRunId,
        )}/health-check`,
        { body: payload },
      );
    },
    listProviderHealth(
      projectId: string,
      options?: { providerAccountId?: string; limit?: number; offset?: number },
    ) {
      return request<ProviderHealthListResponse>("GET", "/v1/provider-health", {
        query: {
          project_id: projectId,
          provider_account_id: options?.providerAccountId,
          limit: options?.limit,
          offset: options?.offset,
        },
      });
    },
    listDriftReports(projectId: string, limit = 100, offset = 0) {
      return request<DriftReportListResponse>("GET", "/v1/drift-reports", {
        query: { project_id: projectId, limit, offset },
      });
    },
    listReconciliationRuns(projectId: string, limit = 100, offset = 0) {
      return request<ReconciliationRunListResponse>(
        "GET",
        "/v1/reconciliation-runs",
        { query: { project_id: projectId, limit, offset } },
      );
    },
    listOutboxMessages(projectId: string, limit = 50, offset = 0) {
      return request<OutboxMessageListResponse>("GET", "/v1/comms/outbox", {
        query: { project_id: projectId, limit, offset },
      });
    },
    listCommsChannelPolicies(projectId: string) {
      return request<CommsChannelPolicyListResponse>("GET", "/v1/comms/policies", {
        query: { project_id: projectId },
      });
    },
    upsertCommsChannelPolicy(payload: CommsChannelPolicyUpsertRequest) {
      return request<CommsChannelPolicySummary>("POST", "/v1/comms/policies", {
        body: payload,
      });
    },
    createOutboxMessage(payload: OutboxMessageCreateRequest) {
      return request<OutboxMessageSummary>("POST", "/v1/comms/outbox", {
        body: payload,
      });
    },
    queueOutboxDelivery(messageId: string, payload: OutboxQueueOnlyRequest) {
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
    dryRunOutboxMessage(messageId: string, payload: OutboxDryRunRequest) {
      return request<OutboxDryRunResponse>(
        "POST",
        `/v1/comms/outbox/${encodeURIComponent(messageId)}/dry-run`,
        { body: payload },
      );
    },
    listInboxMessages(projectId: string, limit = 50, offset = 0) {
      return request<InboxMessageListResponse>("GET", "/v1/comms/inbox", {
        query: { project_id: projectId, limit, offset },
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
    getBudgetForecast(scopeType: string, scopeId: string, horizonHours = 24) {
      return request<BudgetForecastResponse>("GET", "/v1/budget/forecast", {
        query: {
          scope_type: scopeType,
          scope_id: scopeId,
          horizon_hours: horizonHours,
        },
      });
    },
    listBudgetAllocations(projectId: string) {
      return request<BudgetAllocationListResponse>("GET", "/v1/budget/allocations", {
        query: { project_id: projectId },
      });
    },
    createBudgetAllocation(payload: BudgetAllocationCreateRequest) {
      return request<BudgetAllocationSummary>("POST", "/v1/budget/allocations", {
        body: payload,
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
    consumeBudget(payload: BudgetConsumeRequest) {
      return request<BudgetConsumeResponse>("POST", "/v1/budget/consume", {
        body: payload,
      });
    },
    listDecisions(projectId: string, limit?: number, offset = 0) {
      return request<DecisionInboxResponse>("POST", "/v1/decisions/inbox/load", {
        query: { project_id: projectId, limit, offset },
      });
    },
    getDecision(decisionId: string, projectId?: string) {
      return request<DecisionCard>(
        "GET",
        `/v1/decisions/${encodeURIComponent(decisionId)}`,
        { query: { project_id: projectId } },
      );
    },
    createDecisionDigest(payload: DecisionDigestRequest) {
      return request<DecisionDigestResponse>("POST", "/v1/decisions/digests", {
        body: payload,
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
    proposeCIConfig(payload: CIConfigProposalRequest) {
      return request<CIConfigProposalResponse>(
        "POST",
        "/v1/pipeline-config/proposals",
        { body: payload },
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
    listDeploymentRollouts(projectId: string, limit = 20) {
      return request<DeploymentRolloutListResponse>(
        "GET",
        "/v1/software-factory/rollouts",
        { query: { project_id: projectId, limit } },
      );
    },
    requestInfraProvisionReview(payload: InfraProvisionReviewRequest) {
      return request<InfraProvisionReviewResponse>(
        "POST",
        "/v1/infra/droplets/requests",
        {
          body: payload,
        },
      );
    },
    createInfraDroplet(payload: InfraProvisionRequest) {
      return request<InfraProvisionResponse>("POST", "/v1/infra/droplets", {
        body: payload,
      });
    },
    queueInfraDroplet(payload: InfraProvisionJobRequest) {
      return request<JobSummary>("POST", "/v1/infra/droplets/queue", {
        body: payload,
      });
    },
    listInfraResources(projectId: string, limit = 20) {
      return request<InfraResourceListResponse>("GET", "/v1/infra/resources", {
        query: { project_id: projectId, limit },
      });
    },
    terminateInfraResource(resourceId: string, payload: InfraTerminationRequest) {
      return request<InfraTerminationResponse>(
        "POST",
        `/v1/infra/resources/${encodeURIComponent(resourceId)}/terminate`,
        {
          body: payload,
        },
      );
    },
    queueInfraResourceTermination(
      resourceId: string,
      payload: InfraTerminationJobRequest,
    ) {
      return request<JobSummary>(
        "POST",
        `/v1/infra/resources/${encodeURIComponent(resourceId)}/terminate/queue`,
        {
          body: payload,
        },
      );
    },
    runInfraJanitor(payload: InfraJanitorRunRequest) {
      return request<InfraJanitorRunResponse>("POST", "/v1/infra/janitor/run", {
        body: payload,
      });
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
    listReceipts(projectId: string, limit = 50, offset = 0) {
      return request<ReceiptListResponse>("GET", "/v1/receipts", {
        query: { project_id: projectId, limit, offset },
      });
    },
    summarizeAuditTrail(projectId: string, actor?: string) {
      return request<AuditSummaryResponse>("GET", "/v1/audit/summary", {
        query: { project_id: projectId, actor },
      });
    },
    exportAuditTrail(
      projectId: string,
      scope?: { taskId?: string; decisionId?: string; jobId?: string; actor?: string },
    ) {
      return request<AuditExportResponse>("GET", "/v1/audit/export", {
        query: {
          project_id: projectId,
          task_id: scope?.taskId,
          decision_id: scope?.decisionId,
          job_id: scope?.jobId,
          actor: scope?.actor,
        },
      });
    },
    createReplayBundle(payload: ReplayBundleRequest) {
      return request<ReplayBundleResponse>("POST", "/v1/audit/replay-bundles", {
        body: payload,
      });
    },
    createComplianceEvidenceBundle(payload: ComplianceEvidenceBundleRequest) {
      return request<ComplianceEvidenceBundleResponse>(
        "POST",
        "/v1/compliance/evidence-bundles",
        { body: payload },
      );
    },
    listEvents(projectId: string, limit = 50, offset = 0) {
      return request<EventFeedResponse>("GET", "/v1/events", {
        query: { project_id: projectId, limit, offset },
      });
    },
    listDeadletters(projectId: string, limit = 50, offset = 0) {
      return request<DeadletterListResponse>("GET", "/v1/deadletters", {
        query: { project_id: projectId, limit, offset },
      });
    },
    createDeadletterIncident(
      deadletterId: string,
      payload: DeadletterIncidentRequest,
    ) {
      return request<DeadletterIncidentResponse>(
        "POST",
        `/v1/deadletters/${deadletterId}/incident`,
        { body: payload },
      );
    },
    requeueDeadletterJob(deadletterId: string, payload: DeadletterRequeueRequest) {
      return request<DeadletterRequeueResponse>(
        "POST",
        `/v1/deadletters/${deadletterId}/requeue`,
        { body: payload },
      );
    },
    listDeadletterRemediations(
      projectId: string,
      options?: { deadletterId?: string; limit?: number },
    ) {
      return request<DeadletterRemediationListResponse>(
        "GET",
        "/v1/deadletters/remediations",
        {
          query: {
            project_id: projectId,
            deadletter_id: options?.deadletterId,
            limit: options?.limit ?? 100,
          },
        },
      );
    },
    createDeadletterRemediation(
      deadletterId: string,
      payload: DeadletterRemediationCreateRequest,
    ) {
      return request<DeadletterRemediationCreateResponse>(
        "POST",
        `/v1/deadletters/${deadletterId}/remediations`,
        { body: payload },
      );
    },
    retryDeadletter(deadletterId: string, payload: DeadletterRetryRequest) {
      return request<DeadletterRemediationCreateResponse>(
        "POST",
        `/v1/deadletters/${deadletterId}/retry`,
        { body: payload },
      );
    },
    listHardwarePlaces(projectId: string) {
      return request<HardwarePlacesResponse>("POST", "/v1/hardware/places/load", {
        query: { project_id: projectId },
      });
    },
    registerLocalHardwarePlace(payload: HardwareLocalPlaceRequest) {
      return request<HardwarePlace>("POST", "/v1/hardware/places/local", {
        body: payload,
      });
    },
    enrollHardwareAgent(payload: HardwareAgentEnrollRequest) {
      return request<HardwareAgentEnrollResponse>(
        "POST",
        "/v1/hardware/places/agents",
        { body: payload },
      );
    },
    registerHardwareProvider(payload: HardwareProviderPlaceRequest) {
      return request<HardwarePlace>("POST", "/v1/hardware/places/providers", {
        body: payload,
      });
    },
    /** Local place when `placeId` is omitted. */
    resolveHardwarePlace(projectId: string, placeId?: string) {
      return request<HardwarePlace>("POST", "/v1/hardware/resolve/load", {
        query: { project_id: projectId, place_id: placeId },
      });
    },
    listWorkflowWebhooks(projectId: string, workflowKey?: string) {
      return request<{ webhooks: WorkflowWebhookTrigger[] }>(
        "GET",
        "/v1/delivery-workflows/webhooks",
        { query: { project_id: projectId, workflow_key: workflowKey } },
      );
    },
    createWorkflowWebhook(payload: WorkflowWebhookTriggerCreateRequest) {
      return request<WorkflowWebhookTriggerWithSecret>(
        "POST",
        "/v1/delivery-workflows/webhooks",
        { body: payload },
      );
    },
    rotateWorkflowWebhookSecret(projectId: string, triggerId: string, actor?: string) {
      return request<WorkflowWebhookTriggerWithSecret>(
        "POST",
        `/v1/delivery-workflows/webhooks/${encodeURIComponent(triggerId)}/rotate`,
        { body: { project_id: projectId, ...(actor ? { actor } : {}) } },
      );
    },
    setWorkflowWebhookEnabled(projectId: string, triggerId: string, enabled: boolean, actor?: string) {
      return request<WorkflowWebhookTrigger>(
        "POST",
        `/v1/delivery-workflows/webhooks/${encodeURIComponent(triggerId)}/enabled`,
        { body: { project_id: projectId, enabled, ...(actor ? { actor } : {}) } },
      );
    },
    listWorkflowWebhookDeliveries(projectId: string, triggerId: string, limit = 50) {
      return request<WorkflowWebhookDeliveryList>(
        "GET",
        `/v1/delivery-workflows/webhooks/${encodeURIComponent(triggerId)}/deliveries`,
        { query: { project_id: projectId, limit } },
      );
    },
    // KNOWLEDGE GRAPH VIEW (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3; Phase 0 contract).
    getKnowledgeViewOverview(projectId: string, options: { actor?: string; layers?: string; repo?: string; area?: string } = {}) {
      return request<KgOverviewResponse>("GET", "/v1/knowledge/view/overview", {
        query: { project_id: projectId, ...options },
      });
    },
    getKnowledgeViewNeighbourhood(projectId: string, node: string, options: { actor?: string; depth?: number; layers?: string; limit?: number } = {}) {
      return request<KgNeighbourhoodResponse>("GET", "/v1/knowledge/view/neighbourhood", {
        query: { project_id: projectId, node, ...options },
      });
    },
    getKnowledgeViewNode(projectId: string, nodeId: string, options: { actor?: string; edge?: string; direction?: "out" | "in"; offset?: number } = {}) {
      return request<KgNodeDetailResponse>("GET", `/v1/knowledge/view/node/${encodeURIComponent(nodeId)}`, {
        query: { project_id: projectId, ...options },
      });
    },
    getKnowledgeViewTreeChildren(projectId: string, options: { actor?: string; parent?: string | null; tab?: KgTreeTab; q?: string; offset?: number } = {}) {
      return request<KgTreeChildrenResponse>("GET", "/v1/knowledge/view/tree/children", {
        query: { project_id: projectId, ...options },
      });
    },
    searchKnowledgeView(projectId: string, q: string, options: { actor?: string; layers?: string; limit?: number; meaning?: boolean } = {}) {
      const { meaning, ...rest } = options;
      return request<KgSearchResponse>("GET", "/v1/knowledge/view/search", {
        query: { project_id: projectId, q, ...rest, meaning: meaning ? 1 : undefined },
      });
    },
    listKnowledgeRules(projectId: string, options: { actor?: string; status?: KgRuleStatus; scan_id?: string; area?: string } = {}) {
      return request<KgRulesResponse>("GET", "/v1/knowledge/view/rules", {
        query: { project_id: projectId, ...options },
      });
    },
    approveKnowledgeRule(ruleId: string, payload: KgRuleActionRequest) {
      return request<KgRuleActionResponse>("POST", `/v1/knowledge/view/rules/${encodeURIComponent(ruleId)}/approve`, {
        body: payload,
      });
    },
    editKnowledgeRule(ruleId: string, payload: KgRuleActionRequest) {
      return request<KgRuleActionResponse>("POST", `/v1/knowledge/view/rules/${encodeURIComponent(ruleId)}/edit`, {
        body: payload,
      });
    },
    rejectKnowledgeRule(ruleId: string, payload: KgRuleActionRequest) {
      return request<KgRuleActionResponse>("POST", `/v1/knowledge/view/rules/${encodeURIComponent(ruleId)}/reject`, {
        body: payload,
      });
    },
    getKnowledgeLive(projectId: string, options: { actor?: string; since?: string; run?: string } = {}) {
      return request<KgLiveResponse>("GET", "/v1/knowledge/view/live", {
        query: { project_id: projectId, ...options },
      });
    },
    getKnowledgeRunProof(projectId: string, options: { actor?: string; run: string; baseline?: string }) {
      return request<KgRunProofResponse>("GET", "/v1/knowledge/view/proof", {
        query: { project_id: projectId, ...options },
      });
    },
    archiveProject(projectId: string, payload: ProjectArchiveRequest) {
      return request<ProjectArchiveResponse>("POST", `/v1/projects/${encodeURIComponent(projectId)}/archive`, {
        body: payload,
      });
    },
    unarchiveProject(projectId: string, payload: ProjectArchiveRequest) {
      return request<ProjectArchiveResponse>("POST", `/v1/projects/${encodeURIComponent(projectId)}/unarchive`, {
        body: payload,
      });
    },
  };
}
