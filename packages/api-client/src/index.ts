export type HealthResponse = {
  status: string;
  database_connected: boolean;
  github_configured: boolean;
  openai_configured: boolean;
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
};

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

export type AccountConnectOrgSummary = {
  id: string;
  project_id: string;
  hosted_org_id: string;
  local_org_id: string;
  connection_mode: string;
  marketplace_identity: Record<string, unknown>;
  billing_portal_ref?: string;
  connector_authorization_refs: string[];
  optional_telemetry_enabled: boolean;
  connection_ref?: string;
  status: string;
  linked_by: string;
  linked_at?: string;
  unlinked_by?: string;
  unlinked_at?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type EditionStatusResponse = {
  project_id: string;
  edition_id: string;
  display_name: string;
  status: string;
  account_connect_required: boolean;
  account_connect_status: string;
  account_connect_org?: AccountConnectOrgSummary;
  local_auth_allowed: boolean;
  hosted_auth_required: boolean;
  connection_mode: string;
  license_candidate?: string;
  legal_review_required: boolean;
  included_capabilities: string[];
  commercial_capabilities: string[];
  blocked_reason?: string;
  source_pack: string;
};

export type LicenseEntitlementSummary = {
  id: string;
  project_id: string;
  edition_id: string;
  account_connect_org_id?: string;
  scope_type: string;
  scope_id: string;
  capability_key: string;
  capabilities: string[];
  seats?: number;
  status: string;
  source: string;
  expires_at?: string;
  metadata: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type LicenseEntitlementListResponse = {
  project_id: string;
  edition: EditionStatusResponse;
  items: LicenseEntitlementSummary[];
};

export type AccountConnectLinkRequest = {
  project_id: string;
  hosted_org_id: string;
  connection_ref?: string;
  marketplace_identity?: Record<string, unknown>;
  billing_portal_ref?: string;
  connector_authorization_refs?: string[];
  optional_telemetry_enabled?: boolean;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectUnlinkRequest = {
  project_id: string;
  hosted_org_id?: string;
  reason?: string;
  actor?: string;
};

export type AccountConnectMutationResponse = {
  project_id: string;
  edition: EditionStatusResponse;
};

export type AccountConnectSyncRequest = {
  project_id: string;
  hosted_org_id?: string;
  actor?: string;
};

export type AccountConnectSyncResponse = {
  project_id: string;
  edition: EditionStatusResponse;
  synced_entitlement_count: number;
};

export type AccountConnectHealthCheckSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  check_kind: string;
  status: string;
  latency_ms?: number;
  provider_ref?: string;
  readiness_packet_artifact_id?: string;
  response_metadata: Record<string, unknown>;
  error?: Record<string, unknown>;
  checked_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type AccountConnectHealthCheckListResponse = {
  project_id: string;
  items: AccountConnectHealthCheckSummary[];
};

export type AccountConnectHealthCheckRunRequest = {
  project_id: string;
  hosted_org_id?: string;
  check_kind?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectAuthSessionSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  authorization_url?: string;
  return_url?: string;
  provider_ref?: string;
  state: string;
  status: string;
  receipt_id?: string;
  created_by: string;
  expires_at?: string;
  completed_at?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AccountConnectAuthSessionListResponse = {
  project_id: string;
  items: AccountConnectAuthSessionSummary[];
};

export type AccountConnectAuthSessionCreateRequest = {
  project_id: string;
  hosted_org_id?: string;
  return_url?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectAuthSessionCompleteRequest = {
  project_id: string;
  code: string;
  state: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectTeamInviteSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id: string;
  email: string;
  display_name?: string;
  role_keys: string[];
  permissions: string[];
  invite_url?: string;
  hosted_invite_ref?: string;
  status: string;
  delivery_status: string;
  receipt_id?: string;
  created_by: string;
  accepted_by?: string;
  accepted_at?: string;
  revoked_by?: string;
  revoked_at?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AccountConnectTeamInviteListResponse = {
  project_id: string;
  items: AccountConnectTeamInviteSummary[];
};

export type AccountConnectTeamInviteCreateRequest = {
  project_id: string;
  email: string;
  display_name?: string;
  role_keys?: string[];
  permissions?: string[];
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type AccountConnectTeamInviteRevokeRequest = {
  project_id: string;
  reason?: string;
  actor?: string;
};

export type AccountConnectBillingPortalSessionSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id: string;
  portal_url?: string;
  return_url?: string;
  provider_ref?: string;
  status: string;
  receipt_id?: string;
  created_by: string;
  expires_at?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AccountConnectBillingPortalSessionListResponse = {
  project_id: string;
  items: AccountConnectBillingPortalSessionSummary[];
};

export type AccountConnectBillingPortalSessionCreateRequest = {
  project_id: string;
  return_url?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseClusterSummary = {
  id: string;
  org_id: string;
  project_id: string;
  deployment_mode: string;
  account_connect_mode: string;
  worker_pool_strategy: string;
  ha_profile: string;
  data_residency: string;
  identity_controls: Record<string, unknown>;
  governance_controls: Record<string, unknown>;
  audit_controls: Record<string, unknown>;
  private_worker_pools: Array<Record<string, unknown>>;
  compliance_packs: string[];
  support_tier: string;
  support_sla: Record<string, unknown>;
  status: string;
  blocking_gaps: string[];
  created_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type EnterpriseClusterListResponse = {
  project_id: string;
  items: EnterpriseClusterSummary[];
};

export type EnterpriseClusterValidationRunSummary = {
  id: string;
  org_id: string;
  project_id: string;
  enterprise_cluster_id: string;
  validation_kind: string;
  status: string;
  checks: Array<Record<string, unknown>>;
  evidence_refs: string[];
  blocking_gaps: string[];
  validated_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type EnterpriseClusterValidationRunListResponse = {
  project_id: string;
  items: EnterpriseClusterValidationRunSummary[];
};

export type EnterpriseClusterValidationRunCreateRequest = {
  project_id: string;
  enterprise_cluster_id: string;
  validation_kind: string;
  status?: string;
  checks: Array<Record<string, unknown>>;
  evidence_refs?: string[];
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseClusterUpsertRequest = {
  project_id: string;
  deployment_mode?: string;
  account_connect_mode?: string;
  worker_pool_strategy?: string;
  ha_profile?: string;
  data_residency?: string;
  identity_controls?: Record<string, unknown>;
  governance_controls?: Record<string, unknown>;
  audit_controls?: Record<string, unknown>;
  private_worker_pools?: Array<Record<string, unknown>>;
  compliance_packs?: string[];
  support_tier?: string;
  support_sla?: Record<string, unknown>;
  status?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseSupportCaseSummary = {
  id: string;
  org_id: string;
  project_id: string;
  enterprise_cluster_id?: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  title: string;
  severity: string;
  category: string;
  description: string;
  status: string;
  sla_due_at?: string;
  response_time_hours: number;
  hosted_case_ref?: string;
  receipt_id?: string;
  support_packet_artifact_id?: string;
  assigned_to?: string;
  created_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type EnterpriseSupportCaseListResponse = {
  project_id: string;
  items: EnterpriseSupportCaseSummary[];
};

export type EnterpriseSupportCaseCreateRequest = {
  project_id: string;
  title: string;
  description: string;
  severity?: string;
  category?: string;
  enterprise_cluster_id?: string;
  escalate_to_hosted?: boolean;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type EnterpriseSupportCaseUpdateRequest = {
  project_id: string;
  status?: string;
  assigned_to?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

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
};

export type TaskSummary = {
  id: string;
  project_id: string;
  title: string;
  description?: string;
  status: string;
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
  state: CodeTaskState;
  usage?: { cost_usd_micros?: number; model?: string };
  reservation_id?: string | null;
  pull_request_url?: string | null;
  pull_request_number?: number | null;
  decision_id?: string | null;
  error?: string | null;
};

export type CodeTasksResponse = { code_tasks: CodeTaskRun[] };

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

export type SandboxesResponse = { sandboxes: SandboxSummary[]; unavailable?: string };

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
  synthesis?: string | null;
  prd_markdown?: string | null;
  pull_request_url?: string | null;
  pull_request_number?: number | null;
};

export type MeetingsResponse = {
  project_id: string;
  items: Meeting[];
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
};

export type DeploymentRolloutListResponse = {
  project_id: string;
  items: DeploymentRolloutSummary[];
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

export type OutboxDeliveryResponse = {
  status: string;
  data_boundary?: DataBoundary;
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

export type SecretRefSummary = {
  id: string;
  project_id: string;
  ref_kind: string;
  ref_key: string;
  provider?: string;
  status: string;
  metadata: Record<string, unknown>;
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
  actor?: string;
};

export type SecretRefUpdateRequest = {
  project_id: string;
  provider?: string;
  status?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
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

export type HostedMarketplaceListingSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  listing_id: string;
  pack_id: string;
  version: string;
  display_name: string;
  publisher: string;
  category: string;
  description?: string;
  status: string;
  price_model?: string;
  price_usd_micros?: number;
  required_entitlements: string[];
  capabilities: string[];
  source_url?: string;
  metadata: Record<string, unknown>;
  last_sync_run_id?: string;
  created_at: string;
  updated_at: string;
};

export type HostedMarketplaceListingListResponse = {
  project_id: string;
  items: HostedMarketplaceListingSummary[];
};

export type HostedMarketplaceSyncRunSummary = {
  id: string;
  project_id: string;
  account_connect_org_id?: string;
  hosted_org_id?: string;
  status: string;
  listing_count: number;
  provider_ref?: string;
  sync_packet_artifact_id?: string;
  error?: Record<string, unknown>;
  synced_by: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type HostedMarketplaceSyncRunListResponse = {
  project_id: string;
  items: HostedMarketplaceSyncRunSummary[];
};

export type HostedMarketplaceSyncRequest = {
  project_id: string;
  hosted_org_id?: string;
  metadata?: Record<string, unknown>;
  actor?: string;
};

export type HostedMarketplaceSyncResponse = {
  project_id: string;
  sync_run: HostedMarketplaceSyncRunSummary;
  listing_count: number;
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

export type ProviderAccountSummary = {
  id: string;
  project_id: string;
  provider: string;
  account_label: string;
  credential_ref?: string;
  scopes: string[];
  status: string;
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
  credential_ref?: string;
  scopes?: string[];
  read_only?: boolean;
  deploy_capable?: boolean;
  billing_readable?: boolean;
  destructive_actions_disabled?: boolean;
  actor?: string;
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
  secret_refs: string[];
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
  secret_refs?: string[];
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

export function createBossApiClient(config: BossApiClientConfig) {
  const baseUrl = config.baseUrl.replace(/\/$/, "");
  const fetchImpl = config.fetchImpl ?? fetch;
  const apiKey = config.apiKey?.trim();

  async function request<T>(
    method: "GET" | "POST" | "PATCH" | "DELETE",
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
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
        ...(options?.body ? { "Content-Type": "application/json" } : {}),
      },
      ...(options?.body ? { body: JSON.stringify(options.body) } : {}),
    });

    if (!response.ok) {
      const rawBody = await response.text();
      let body: unknown = rawBody;
      try {
        body = rawBody ? JSON.parse(rawBody) : undefined;
      } catch {}
      throw new BossApiError(
        `BOSS API request failed: ${response.status}`,
        response.status,
        body,
      );
    }

    if (response.status === 204) {
      return undefined as T;
    }
    const text = await response.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  return {
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
    getMeetingRoles(projectId: string) {
      return request<MeetingRolesResponse>("GET", "/v1/meeting-roles", {
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
      return request<{ status: string; task_id: string; job_id: string }>(
        "POST", "/v1/meetings/prd", { body: payload },
      );
    },
    getCodeTasks(projectId: string, limit = 50) {
      return request<CodeTasksResponse>("GET", "/v1/code-tasks", {
        query: { project_id: projectId, limit },
      });
    },
    getCodeTask(runId: string) {
      return request<CodeTaskRun>("GET", `/v1/code-tasks/${encodeURIComponent(runId)}`);
    },
    createCodeTask(payload: CodeTaskCreatePayload) {
      return request<{ status: string; run_id: string }>("POST", "/v1/code-tasks", { body: payload });
    },
    getSandboxes() {
      return request<SandboxesResponse>("GET", "/v1/sandboxes");
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
    deleteMcpServer(serverId: string, projectId: string) {
      return request<void>("DELETE", `/v1/mcp/servers/${encodeURIComponent(serverId)}`, {
        query: { project_id: projectId },
      });
    },
    getMcpServers(projectId: string) {
      return request<McpServersResponse>("GET", "/v1/mcp/servers", {
        query: { project_id: projectId },
      });
    },
    getHealth() {
      return request<HealthResponse>("GET", "/health");
    },
    listProjects() {
      return request<ProjectListResponse>("GET", "/v1/projects");
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
    listLicenseEntitlements(projectId: string, includeInactive = false) {
      return request<LicenseEntitlementListResponse>("GET", "/v1/entitlements", {
        query: {
          project_id: projectId,
          include_inactive: String(includeInactive),
        },
      });
    },
    linkAccountConnect(payload: AccountConnectLinkRequest) {
      return request<AccountConnectMutationResponse>(
        "POST",
        "/v1/account-connect/link",
        { body: payload },
      );
    },
    unlinkAccountConnect(payload: AccountConnectUnlinkRequest) {
      return request<AccountConnectMutationResponse>(
        "POST",
        "/v1/account-connect/unlink",
        { body: payload },
      );
    },
    syncAccountConnect(payload: AccountConnectSyncRequest) {
      return request<AccountConnectSyncResponse>(
        "POST",
        "/v1/account-connect/sync",
        { body: payload },
      );
    },
    listAccountConnectHealthChecks(projectId: string, limit = 50) {
      return request<AccountConnectHealthCheckListResponse>(
        "GET",
        "/v1/account-connect/health-checks",
        { query: { project_id: projectId, limit: String(limit) } },
      );
    },
    runAccountConnectHealthCheck(payload: AccountConnectHealthCheckRunRequest) {
      return request<AccountConnectHealthCheckSummary>(
        "POST",
        "/v1/account-connect/health-checks",
        { body: payload },
      );
    },
    listAccountConnectAuthSessions(projectId: string, limit = 50) {
      return request<AccountConnectAuthSessionListResponse>(
        "GET",
        "/v1/account-connect/auth-sessions",
        { query: { project_id: projectId, limit: String(limit) } },
      );
    },
    createAccountConnectAuthSession(
      payload: AccountConnectAuthSessionCreateRequest,
    ) {
      return request<AccountConnectAuthSessionSummary>(
        "POST",
        "/v1/account-connect/auth-sessions",
        { body: payload },
      );
    },
    completeAccountConnectAuthSession(
      sessionId: string,
      payload: AccountConnectAuthSessionCompleteRequest,
    ) {
      return request<AccountConnectAuthSessionSummary>(
        "POST",
        `/v1/account-connect/auth-sessions/${encodeURIComponent(sessionId)}/complete`,
        { body: payload },
      );
    },
    listAccountConnectTeamInvites(projectId: string, limit = 50) {
      return request<AccountConnectTeamInviteListResponse>(
        "GET",
        "/v1/account-connect/team-invites",
        { query: { project_id: projectId, limit: String(limit) } },
      );
    },
    createAccountConnectTeamInvite(
      payload: AccountConnectTeamInviteCreateRequest,
    ) {
      return request<AccountConnectTeamInviteSummary>(
        "POST",
        "/v1/account-connect/team-invites",
        { body: payload },
      );
    },
    revokeAccountConnectTeamInvite(
      inviteId: string,
      payload: AccountConnectTeamInviteRevokeRequest,
    ) {
      return request<AccountConnectTeamInviteSummary>(
        "POST",
        `/v1/account-connect/team-invites/${encodeURIComponent(inviteId)}/revoke`,
        { body: payload },
      );
    },
    listAccountConnectBillingPortalSessions(projectId: string, limit = 50) {
      return request<AccountConnectBillingPortalSessionListResponse>(
        "GET",
        "/v1/account-connect/billing-portal-sessions",
        { query: { project_id: projectId, limit: String(limit) } },
      );
    },
    createAccountConnectBillingPortalSession(
      payload: AccountConnectBillingPortalSessionCreateRequest,
    ) {
      return request<AccountConnectBillingPortalSessionSummary>(
        "POST",
        "/v1/account-connect/billing-portal-sessions",
        { body: payload },
      );
    },
    listEnterpriseClusters(projectId: string) {
      return request<EnterpriseClusterListResponse>(
        "GET",
        "/v1/enterprise/clusters",
        { query: { project_id: projectId } },
      );
    },
    upsertEnterpriseCluster(payload: EnterpriseClusterUpsertRequest) {
      return request<EnterpriseClusterSummary>(
        "POST",
        "/v1/enterprise/clusters",
        { body: payload },
      );
    },
    listEnterpriseClusterValidationRuns(
      projectId: string,
      enterpriseClusterId?: string,
      limit = 50,
    ) {
      return request<EnterpriseClusterValidationRunListResponse>(
        "GET",
        "/v1/enterprise/cluster-validation-runs",
        {
          query: {
            project_id: projectId,
            ...(enterpriseClusterId
              ? { enterprise_cluster_id: enterpriseClusterId }
              : {}),
            limit,
          },
        },
      );
    },
    createEnterpriseClusterValidationRun(
      payload: EnterpriseClusterValidationRunCreateRequest,
    ) {
      return request<EnterpriseClusterValidationRunSummary>(
        "POST",
        "/v1/enterprise/cluster-validation-runs",
        { body: payload },
      );
    },
    listEnterpriseSupportCases(projectId: string, limit = 50) {
      return request<EnterpriseSupportCaseListResponse>(
        "GET",
        "/v1/enterprise/support-cases",
        { query: { project_id: projectId, limit } },
      );
    },
    createEnterpriseSupportCase(payload: EnterpriseSupportCaseCreateRequest) {
      return request<EnterpriseSupportCaseSummary>(
        "POST",
        "/v1/enterprise/support-cases",
        { body: payload },
      );
    },
    updateEnterpriseSupportCase(
      caseId: string,
      payload: EnterpriseSupportCaseUpdateRequest,
    ) {
      return request<EnterpriseSupportCaseSummary>(
        "POST",
        `/v1/enterprise/support-cases/${encodeURIComponent(caseId)}`,
        { body: payload },
      );
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
    listChangeJournal(projectId: string, limit = 25) {
      return request<ChangeJournalResponse>("GET", "/v1/change-journal", {
        query: { project_id: projectId, limit },
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
    listHostedMarketplaceListings(
      projectId: string,
      includeInactive = false,
      limit = 50,
    ) {
      return request<HostedMarketplaceListingListResponse>(
        "GET",
        "/v1/marketplace/listings",
        {
          query: {
            project_id: projectId,
            include_inactive: includeInactive,
            limit: String(limit),
          },
        },
      );
    },
    listHostedMarketplaceSyncRuns(projectId: string, limit = 50) {
      return request<HostedMarketplaceSyncRunListResponse>(
        "GET",
        "/v1/marketplace/sync-runs",
        {
          query: { project_id: projectId, limit: String(limit) },
        },
      );
    },
    syncHostedMarketplace(payload: HostedMarketplaceSyncRequest) {
      return request<HostedMarketplaceSyncResponse>(
        "POST",
        "/v1/marketplace/sync",
        { body: payload },
      );
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
    listSkills() {
      return request<SkillListResponse>("GET", "/v1/skills");
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
    createProviderAccount(payload: ProviderAccountCreateRequest) {
      return request<ProviderAccountSummary>("POST", "/v1/providers", {
        body: payload,
      });
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
    listDecisions(projectId: string) {
      return request<DecisionInboxResponse>("GET", "/v1/decisions/inbox", {
        query: { project_id: projectId },
      });
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
  };
}
