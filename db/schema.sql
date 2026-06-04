-- BOSS starter schema v0.1
-- Intended for Postgres. Codex should convert this into migrations in the chosen backend stack.

CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, slug)
);

CREATE TABLE IF NOT EXISTS project_members (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  member_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  member_type TEXT NOT NULL DEFAULT 'human',
  role_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
  permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT 'bootstrap',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_project_members_project
  ON project_members(project_id, member_id);

CREATE TABLE IF NOT EXISTS org_members (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  member_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  member_type TEXT NOT NULL DEFAULT 'human',
  role_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
  permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT 'bootstrap',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(org_id, member_id)
);
CREATE INDEX IF NOT EXISTS idx_org_members_org
  ON org_members(org_id, member_id, status);

CREATE TABLE IF NOT EXISTS break_glass_grants (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  target_member_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  scope JSONB NOT NULL DEFAULT '{}'::jsonb,
  granted_permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  expires_at TIMESTAMPTZ NOT NULL,
  postmortem_task_id TEXT,
  revoked_by TEXT,
  revoked_reason TEXT,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_break_glass_grants_project_target
  ON break_glass_grants(project_id, target_member_id, status, expires_at);

CREATE TABLE IF NOT EXISTS roles (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  role_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  autonomy_level INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, role_key)
);
ALTER TABLE roles ADD COLUMN IF NOT EXISTS trust_score INTEGER NOT NULL DEFAULT 50;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS successful_runs INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS policy_violation_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS duplicate_effect_attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS hidden_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS low_quality_output_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS scope_violation_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS last_reviewed_at TIMESTAMPTZ;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS last_violation_at TIMESTAMPTZ;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS source_module_id TEXT;
ALTER TABLE roles ADD COLUMN IF NOT EXISTS source_module_version TEXT;

CREATE TABLE IF NOT EXISTS pack_installs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  pack_id TEXT NOT NULL,
  pack_version TEXT NOT NULL,
  description TEXT,
  spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  installed_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, pack_id)
);

CREATE TABLE IF NOT EXISTS agent_module_installs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  module_id TEXT NOT NULL,
  module_version TEXT NOT NULL,
  role_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  publisher TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'general',
  description TEXT,
  marketplace JSONB NOT NULL DEFAULT '{}'::jsonb,
  runtime JSONB NOT NULL DEFAULT '{}'::jsonb,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  installed_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, module_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_module_installs_project
  ON agent_module_installs(project_id, installed_at DESC);

CREATE TABLE IF NOT EXISTS agent_module_install_records (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  module_id TEXT NOT NULL,
  action TEXT NOT NULL,
  from_version TEXT,
  to_version TEXT,
  actor TEXT NOT NULL,
  status TEXT NOT NULL,
  entitlement_result JSONB NOT NULL DEFAULT '{}'::jsonb,
  rollback_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_module_install_records_project_module
  ON agent_module_install_records(project_id, module_id, created_at DESC);

CREATE TABLE IF NOT EXISTS account_connect_orgs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  hosted_org_id TEXT NOT NULL,
  local_org_id TEXT NOT NULL,
  connection_mode TEXT NOT NULL DEFAULT 'attached',
  marketplace_identity JSONB NOT NULL DEFAULT '{}'::jsonb,
  billing_portal_ref TEXT,
  connector_authorization_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  optional_telemetry_enabled BOOLEAN NOT NULL DEFAULT false,
  connection_ref TEXT,
  status TEXT NOT NULL DEFAULT 'linked',
  linked_by TEXT NOT NULL DEFAULT 'system',
  linked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  unlinked_by TEXT,
  unlinked_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, hosted_org_id)
);
CREATE INDEX IF NOT EXISTS idx_account_connect_orgs_project_status
  ON account_connect_orgs(project_id, status, linked_at DESC);

CREATE TABLE IF NOT EXISTS account_connect_auth_sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  account_connect_org_id TEXT REFERENCES account_connect_orgs(id) ON DELETE SET NULL,
  hosted_org_id TEXT,
  authorization_url TEXT,
  return_url TEXT,
  provider_ref TEXT,
  state TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'requested',
  receipt_id TEXT REFERENCES receipts(id),
  created_by TEXT NOT NULL DEFAULT 'system',
  expires_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_connect_auth_sessions_project
  ON account_connect_auth_sessions(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_connect_auth_sessions_provider_ref
  ON account_connect_auth_sessions(project_id, provider_ref);

CREATE TABLE IF NOT EXISTS license_entitlements (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  edition_id TEXT NOT NULL,
  account_connect_org_id TEXT,
  scope_type TEXT NOT NULL DEFAULT 'project',
  scope_id TEXT NOT NULL,
  capability_key TEXT NOT NULL,
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  seats INTEGER,
  status TEXT NOT NULL DEFAULT 'active',
  source TEXT NOT NULL DEFAULT 'local_edition',
  expires_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, capability_key, source)
);
CREATE INDEX IF NOT EXISTS idx_license_entitlements_project_status
  ON license_entitlements(project_id, status, edition_id);

CREATE TABLE IF NOT EXISTS enterprise_clusters (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deployment_mode TEXT NOT NULL DEFAULT 'enterprise_cloud',
  account_connect_mode TEXT NOT NULL DEFAULT 'attached',
  worker_pool_strategy TEXT NOT NULL DEFAULT 'managed_workers',
  ha_profile TEXT NOT NULL DEFAULT 'standard',
  data_residency TEXT NOT NULL DEFAULT 'us',
  identity_controls JSONB NOT NULL DEFAULT '{}'::jsonb,
  governance_controls JSONB NOT NULL DEFAULT '{}'::jsonb,
  audit_controls JSONB NOT NULL DEFAULT '{}'::jsonb,
  private_worker_pools JSONB NOT NULL DEFAULT '[]'::jsonb,
  compliance_packs JSONB NOT NULL DEFAULT '[]'::jsonb,
  support_tier TEXT NOT NULL DEFAULT 'standard',
  support_sla JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'planned',
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, deployment_mode, account_connect_mode)
);
CREATE INDEX IF NOT EXISTS idx_enterprise_clusters_project_status
  ON enterprise_clusters(project_id, status, deployment_mode);
CREATE INDEX IF NOT EXISTS idx_enterprise_clusters_org
  ON enterprise_clusters(org_id, deployment_mode);

CREATE TABLE IF NOT EXISTS enterprise_cluster_validation_runs (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  enterprise_cluster_id TEXT NOT NULL REFERENCES enterprise_clusters(id) ON DELETE CASCADE,
  validation_kind TEXT NOT NULL,
  status TEXT NOT NULL,
  checks JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  blocking_gaps JSONB NOT NULL DEFAULT '[]'::jsonb,
  validated_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_enterprise_cluster_validation_runs_project
  ON enterprise_cluster_validation_runs(project_id, enterprise_cluster_id, created_at DESC);

CREATE TABLE IF NOT EXISTS enterprise_identity_providers (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider_key TEXT NOT NULL,
  provider_type TEXT NOT NULL,
  display_name TEXT NOT NULL,
  issuer TEXT,
  audience TEXT,
  entity_id TEXT,
  sso_url TEXT,
  jwks_url TEXT,
  certificate_fingerprint TEXT,
  scim_base_url TEXT,
  secret_ref TEXT,
  status TEXT NOT NULL DEFAULT 'planned',
  test_status TEXT NOT NULL DEFAULT 'not_tested',
  blocking_gaps JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, provider_key)
);
CREATE INDEX IF NOT EXISTS idx_enterprise_identity_providers_project_status
  ON enterprise_identity_providers(project_id, status, provider_type);
CREATE INDEX IF NOT EXISTS idx_enterprise_identity_providers_org
  ON enterprise_identity_providers(org_id, provider_type);

CREATE TABLE IF NOT EXISTS enterprise_identity_rollout_policies (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider_type TEXT NOT NULL,
  runtime_mode TEXT NOT NULL DEFAULT 'disabled',
  allowed_domains JSONB NOT NULL DEFAULT '[]'::jsonb,
  rollout_notes TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, provider_type)
);
CREATE INDEX IF NOT EXISTS idx_enterprise_identity_rollout_policies_project
  ON enterprise_identity_rollout_policies(project_id, provider_type, status);

CREATE TABLE IF NOT EXISTS enterprise_saml_sessions (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider_id TEXT NOT NULL REFERENCES enterprise_identity_providers(id) ON DELETE CASCADE,
  member_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  claims JSONB NOT NULL DEFAULT '{}'::jsonb,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_enterprise_saml_sessions_project
  ON enterprise_saml_sessions(project_id, member_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_enterprise_saml_sessions_token
  ON enterprise_saml_sessions(token_hash, status, expires_at);

CREATE TABLE IF NOT EXISTS enterprise_scim_groups (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  external_id TEXT,
  display_name TEXT NOT NULL,
  role_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
  permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  raw_group JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, display_name)
);
CREATE INDEX IF NOT EXISTS idx_enterprise_scim_groups_project_status
  ON enterprise_scim_groups(project_id, status, display_name);

CREATE TABLE IF NOT EXISTS enterprise_scim_users (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  external_id TEXT,
  user_name TEXT NOT NULL,
  display_name TEXT NOT NULL,
  member_id TEXT NOT NULL,
  role_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
  permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  groups JSONB NOT NULL DEFAULT '[]'::jsonb,
  raw_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, user_name)
);
CREATE INDEX IF NOT EXISTS idx_enterprise_scim_users_project_status
  ON enterprise_scim_users(project_id, status, user_name);
CREATE INDEX IF NOT EXISTS idx_enterprise_scim_users_member
  ON enterprise_scim_users(project_id, member_id, status);

CREATE TABLE IF NOT EXISTS entitlement_sources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  entitlement_kind TEXT NOT NULL,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  workload_classes_allowed JSONB NOT NULL DEFAULT '[]'::jsonb,
  multi_tenant_allowed BOOLEAN NOT NULL DEFAULT false,
  credential_ref TEXT,
  cost_mode TEXT NOT NULL DEFAULT 'usage_based_api',
  limit_shape JSONB NOT NULL DEFAULT '{}'::jsonb,
  supported_models JSONB NOT NULL DEFAULT '[]'::jsonb,
  task_strengths JSONB NOT NULL DEFAULT '[]'::jsonb,
  risk_tier_max INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'declared',
  auth_method TEXT NOT NULL DEFAULT 'api_key',
  connection_status TEXT NOT NULL DEFAULT 'not_connected',
  connection_ref TEXT,
  oauth_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  connection_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  policy_notes JSONB NOT NULL DEFAULT '[]'::jsonb,
  observed_capacity JSONB NOT NULL DEFAULT '{}'::jsonb,
  runtime_supported BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, source_id)
);
ALTER TABLE entitlement_sources ADD COLUMN IF NOT EXISTS auth_method TEXT NOT NULL DEFAULT 'api_key';
ALTER TABLE entitlement_sources ADD COLUMN IF NOT EXISTS connection_status TEXT NOT NULL DEFAULT 'not_connected';
ALTER TABLE entitlement_sources ADD COLUMN IF NOT EXISTS connection_ref TEXT;
ALTER TABLE entitlement_sources ADD COLUMN IF NOT EXISTS oauth_scopes JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE entitlement_sources ADD COLUMN IF NOT EXISTS connection_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS capacity_receipts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  entitlement_pool_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  capacity_kind TEXT NOT NULL,
  model_route_decision_id TEXT,
  provider_ref TEXT,
  usage_units JSONB NOT NULL DEFAULT '{}'::jsonb,
  capacity_state_after TEXT NOT NULL DEFAULT 'unknown',
  dedupe_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'recorded',
  observed_capacity JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, entitlement_pool_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_capacity_receipts_project_created
  ON capacity_receipts(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS routing_logs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  skill_id TEXT,
  task_kind TEXT NOT NULL,
  workload_class TEXT NOT NULL,
  risk_tier INTEGER NOT NULL,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  budget_state TEXT NOT NULL,
  selected_source_id TEXT,
  selected_provider TEXT,
  selected_model_id TEXT,
  denial_reason TEXT,
  rejected_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
  decision JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE routing_logs ADD COLUMN IF NOT EXISTS data_boundary TEXT NOT NULL DEFAULT 'internal';

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  owner_role_id TEXT,
  parent_task_id TEXT REFERENCES tasks(id),
  dependency_task_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  due_at TIMESTAMPTZ,
  priority TEXT NOT NULL DEFAULT 'p2',
  risk_tier INTEGER NOT NULL DEFAULT 0,
  inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
  required_artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
  budget_allocation_id TEXT,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  dedupe_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS departments (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  department_key TEXT NOT NULL,
  name TEXT NOT NULL,
  mission TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, department_key)
);

CREATE TABLE IF NOT EXISTS policy_records (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  policy_key TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  body JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, policy_key, version)
);

CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  title TEXT NOT NULL,
  objective TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  phases JSONB NOT NULL DEFAULT '[]'::jsonb,
  success_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
  risks JSONB NOT NULL DEFAULT '[]'::jsonb,
  artifact_id TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meetings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  purpose TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',
  participants JSONB NOT NULL DEFAULT '[]'::jsonb,
  agenda JSONB NOT NULL DEFAULT '[]'::jsonb,
  transcript_artifact_id TEXT,
  minutes_artifact_id TEXT,
  action_task_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
  dissent_records JSONB NOT NULL DEFAULT '[]'::jsonb,
  blocked_state JSONB,
  completed_at TIMESTAMPTZ,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS transcript_artifact_id TEXT;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS action_task_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS dissent_records JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS blocked_state JSONB;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  artifact_type TEXT NOT NULL,
  title TEXT NOT NULL,
  uri TEXT,
  content JSONB,
  content_hash TEXT,
  created_by_job_id TEXT,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS data_boundary TEXT NOT NULL DEFAULT 'internal';

CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  question TEXT NOT NULL,
  recommendation JSONB NOT NULL DEFAULT '{}'::jsonb,
  options JSONB NOT NULL DEFAULT '[]'::jsonb,
  risk_tier INTEGER NOT NULL DEFAULT 1,
  cost_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  deadline_at TIMESTAMPTZ,
  default_action TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  delegated_to TEXT,
  delegated_by TEXT,
  delegated_at TIMESTAMPTZ,
  revision_requested_by TEXT,
  revision_requested_at TIMESTAMPTZ,
  executed_by TEXT,
  executed_at TIMESTAMPTZ,
  resolution_note TEXT,
  external_ref TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS delegated_to TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS delegated_by TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS delegated_at TIMESTAMPTZ;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS revision_requested_by TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS revision_requested_at TIMESTAMPTZ;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS executed_by TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS resolution_note TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS external_ref TEXT;

CREATE TABLE IF NOT EXISTS autonomy_changes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  role_id TEXT NOT NULL REFERENCES roles(id),
  role_key TEXT NOT NULL,
  decision_id TEXT REFERENCES decisions(id),
  change_type TEXT NOT NULL,
  trigger TEXT NOT NULL,
  from_level INTEGER NOT NULL,
  to_level INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_decision',
  reason TEXT NOT NULL,
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_autonomy_changes_project_role
  ON autonomy_changes(project_id, role_key, created_at DESC);

CREATE TABLE IF NOT EXISTS role_reviews (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  role_id TEXT NOT NULL REFERENCES roles(id),
  role_key TEXT NOT NULL,
  summary TEXT NOT NULL,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  trust_score_before INTEGER NOT NULL,
  trust_delta INTEGER NOT NULL,
  trust_score_after INTEGER NOT NULL,
  autonomy_level_before INTEGER NOT NULL,
  autonomy_level_after INTEGER NOT NULL,
  governance_outcome TEXT NOT NULL,
  autonomy_change_id TEXT REFERENCES autonomy_changes(id),
  artifact_id TEXT REFERENCES artifacts(id),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_role_reviews_project_role
  ON role_reviews(project_id, role_key, created_at DESC);

CREATE TABLE IF NOT EXISTS self_improvement_proposals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT NOT NULL REFERENCES tasks(id),
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  change_ref TEXT NOT NULL,
  diff_summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed',
  risk_tier INTEGER NOT NULL DEFAULT 2,
  expected_impact JSONB NOT NULL DEFAULT '[]'::jsonb,
  test_plan JSONB NOT NULL DEFAULT '[]'::jsonb,
  canary_scope JSONB NOT NULL DEFAULT '[]'::jsonb,
  rollback_plan JSONB NOT NULL DEFAULT '[]'::jsonb,
  success_metrics JSONB NOT NULL DEFAULT '[]'::jsonb,
  status_note TEXT,
  proposal_artifact_id TEXT REFERENCES artifacts(id),
  current_decision_id TEXT REFERENCES decisions(id),
  canary_artifact_id TEXT REFERENCES artifacts(id),
  rollout_artifact_id TEXT REFERENCES artifacts(id),
  rollback_artifact_id TEXT REFERENCES artifacts(id),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_self_improvement_proposals_project
  ON self_improvement_proposals(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS health_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  generated_by TEXT NOT NULL,
  budget_state TEXT NOT NULL,
  queue_state JSONB NOT NULL DEFAULT '{}'::jsonb,
  workload_state JSONB NOT NULL DEFAULT '{}'::jsonb,
  incident_state JSONB NOT NULL DEFAULT '{}'::jsonb,
  top_risks JSONB NOT NULL DEFAULT '[]'::jsonb,
  summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_health_snapshots_project
  ON health_snapshots(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  kind TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  status TEXT NOT NULL DEFAULT 'queued',
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  max_attempts INTEGER NOT NULL DEFAULT 3,
  attempt INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_token_hash TEXT,
  lease_expires_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  last_error JSONB,
  run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs (status, run_after, priority, created_at);

CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  project_id TEXT NOT NULL REFERENCES projects(id),
  dedupe_key TEXT NOT NULL,
  effect_kind TEXT NOT NULL,
  effect_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'in_progress',
  provider_ref TEXT,
  result JSONB,
  error JSONB,
  created_by_job_id TEXT REFERENCES jobs(id),
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(org_id, dedupe_key)
);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS data_boundary TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS parent_task_id TEXT REFERENCES tasks(id);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS dependency_task_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_tasks_project_parent ON tasks(project_id, parent_task_id);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS data_boundary TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS data_boundary TEXT NOT NULL DEFAULT 'internal';

CREATE TABLE IF NOT EXISTS enterprise_support_cases (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  enterprise_cluster_id TEXT REFERENCES enterprise_clusters(id) ON DELETE SET NULL,
  account_connect_org_id TEXT REFERENCES account_connect_orgs(id) ON DELETE SET NULL,
  hosted_org_id TEXT,
  title TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'normal',
  category TEXT NOT NULL DEFAULT 'support',
  description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  sla_due_at TIMESTAMPTZ,
  response_time_hours INTEGER NOT NULL DEFAULT 24,
  hosted_case_ref TEXT,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  assigned_to TEXT,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_enterprise_support_cases_project_status
  ON enterprise_support_cases(project_id, status, severity);
CREATE INDEX IF NOT EXISTS idx_enterprise_support_cases_sla
  ON enterprise_support_cases(project_id, sla_due_at);

CREATE TABLE IF NOT EXISTS account_connect_team_invites (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  account_connect_org_id TEXT REFERENCES account_connect_orgs(id) ON DELETE SET NULL,
  hosted_org_id TEXT NOT NULL,
  email TEXT NOT NULL,
  display_name TEXT,
  role_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
  permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  invite_url TEXT,
  hosted_invite_ref TEXT,
  status TEXT NOT NULL DEFAULT 'pending_delivery',
  delivery_status TEXT NOT NULL DEFAULT 'pending',
  receipt_id TEXT REFERENCES receipts(id),
  created_by TEXT NOT NULL DEFAULT 'system',
  accepted_by TEXT,
  accepted_at TIMESTAMPTZ,
  revoked_by TEXT,
  revoked_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_connect_team_invites_project
  ON account_connect_team_invites(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_connect_team_invites_email
  ON account_connect_team_invites(project_id, email, created_at DESC);

CREATE TABLE IF NOT EXISTS account_connect_billing_portal_sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  account_connect_org_id TEXT REFERENCES account_connect_orgs(id) ON DELETE SET NULL,
  hosted_org_id TEXT NOT NULL,
  portal_url TEXT,
  return_url TEXT,
  provider_ref TEXT,
  status TEXT NOT NULL DEFAULT 'requested',
  receipt_id TEXT REFERENCES receipts(id),
  created_by TEXT NOT NULL DEFAULT 'system',
  expires_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_connect_billing_sessions_project
  ON account_connect_billing_portal_sessions(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS account_connect_health_checks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  account_connect_org_id TEXT REFERENCES account_connect_orgs(id) ON DELETE SET NULL,
  hosted_org_id TEXT,
  check_kind TEXT NOT NULL DEFAULT 'hosted_service',
  status TEXT NOT NULL DEFAULT 'unknown',
  latency_ms INTEGER,
  provider_ref TEXT,
  response_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  error JSONB,
  checked_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_connect_health_checks_project
  ON account_connect_health_checks(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS hosted_marketplace_sync_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  account_connect_org_id TEXT REFERENCES account_connect_orgs(id) ON DELETE SET NULL,
  hosted_org_id TEXT,
  status TEXT NOT NULL,
  listing_count INTEGER NOT NULL DEFAULT 0,
  provider_ref TEXT,
  error JSONB,
  synced_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_hosted_marketplace_sync_runs_project
  ON hosted_marketplace_sync_runs(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS hosted_marketplace_pack_listings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  account_connect_org_id TEXT REFERENCES account_connect_orgs(id) ON DELETE SET NULL,
  hosted_org_id TEXT,
  listing_id TEXT NOT NULL,
  pack_id TEXT NOT NULL,
  version TEXT NOT NULL,
  display_name TEXT NOT NULL,
  publisher TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'general',
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  price_model TEXT,
  price_usd_micros BIGINT,
  required_entitlements JSONB NOT NULL DEFAULT '[]'::jsonb,
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_url TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_sync_run_id TEXT REFERENCES hosted_marketplace_sync_runs(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_hosted_marketplace_pack_listings_project
  ON hosted_marketplace_pack_listings(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS deployment_rollouts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  decision_id TEXT REFERENCES decisions(id),
  deployment_receipt_id TEXT REFERENCES receipts(id),
  deployment_artifact_id TEXT REFERENCES artifacts(id),
  rollout_artifact_id TEXT REFERENCES artifacts(id),
  title TEXT NOT NULL,
  environment TEXT NOT NULL,
  merged_sha TEXT NOT NULL,
  pull_request_url TEXT NOT NULL,
  branch_name TEXT,
  status TEXT NOT NULL,
  current_stage TEXT NOT NULL,
  health_verification_status TEXT,
  rollback_decision_id TEXT REFERENCES decisions(id),
  stages JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_by_job_id TEXT REFERENCES jobs(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_deployment_rollouts_project
  ON deployment_rollouts(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS infra_resources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  decision_id TEXT REFERENCES decisions(id),
  provider TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_name TEXT NOT NULL,
  external_resource_id TEXT NOT NULL,
  region TEXT NOT NULL,
  size TEXT NOT NULL,
  image TEXT NOT NULL,
  tags JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  ttl_minutes INTEGER NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  terminated_at TIMESTAMPTZ,
  termination_reason TEXT,
  provision_receipt_id TEXT REFERENCES receipts(id),
  provision_artifact_id TEXT REFERENCES artifacts(id),
  termination_receipt_id TEXT REFERENCES receipts(id),
  termination_artifact_id TEXT REFERENCES artifacts(id),
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by_job_id TEXT REFERENCES jobs(id),
  updated_by_job_id TEXT REFERENCES jobs(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_infra_resources_project
  ON infra_resources(project_id, status, expires_at, created_at DESC);

CREATE TABLE IF NOT EXISTS secret_refs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  ref_kind TEXT NOT NULL DEFAULT 'env',
  ref_key TEXT NOT NULL,
  provider TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, ref_kind, ref_key)
);
CREATE INDEX IF NOT EXISTS idx_secret_refs_project
  ON secret_refs(project_id, provider, status);

CREATE TABLE IF NOT EXISTS provider_adapters (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  adapter_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  adapter_kind TEXT NOT NULL DEFAULT 'official_api',
  status TEXT NOT NULL DEFAULT 'available',
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(provider, adapter_key)
);

CREATE TABLE IF NOT EXISTS provider_accounts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider TEXT NOT NULL,
  account_label TEXT NOT NULL,
  credential_ref TEXT,
  scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  read_only BOOLEAN NOT NULL DEFAULT true,
  deploy_capable BOOLEAN NOT NULL DEFAULT false,
  billing_readable BOOLEAN NOT NULL DEFAULT false,
  destructive_actions_disabled BOOLEAN NOT NULL DEFAULT true,
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  missing_permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  last_test_status TEXT,
  last_tested_at TIMESTAMPTZ,
  last_error JSONB,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, provider, account_label)
);
CREATE INDEX IF NOT EXISTS idx_provider_accounts_project
  ON provider_accounts(project_id, provider, status);

CREATE TABLE IF NOT EXISTS capabilities (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT NOT NULL REFERENCES provider_accounts(id),
  capability_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'available',
  detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(provider_account_id, capability_key)
);

CREATE TABLE IF NOT EXISTS external_resources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  provider TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  external_id TEXT NOT NULL,
  name TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'discovered_unmanaged',
  ownership TEXT NOT NULL DEFAULT 'discovered_unmanaged',
  environment TEXT NOT NULL DEFAULT 'unknown',
  owner_role TEXT,
  task_id TEXT REFERENCES tasks(id),
  deployment_target_id TEXT,
  receipt_id TEXT,
  budget_allocation_id TEXT,
  health_status TEXT NOT NULL DEFAULT 'unknown',
  estimated_monthly_usd_micros BIGINT,
  actual_monthly_usd_micros BIGINT,
  cost_confidence TEXT NOT NULL DEFAULT 'unknown',
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  tags JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_external_resources_project
  ON external_resources(project_id, provider, ownership, state, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS billing_accounts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  provider TEXT NOT NULL,
  external_billing_id TEXT,
  label TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  billing_period JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_billing_accounts_project
  ON billing_accounts(project_id, provider, status);

CREATE TABLE IF NOT EXISTS cost_events (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  external_resource_id TEXT REFERENCES external_resources(id),
  billing_account_id TEXT REFERENCES billing_accounts(id),
  task_id TEXT REFERENCES tasks(id),
  role_key TEXT,
  provider TEXT NOT NULL,
  category TEXT NOT NULL,
  amount_usd_micros BIGINT NOT NULL,
  cost_layer TEXT NOT NULL DEFAULT 'observed',
  confidence TEXT NOT NULL,
  source_ref TEXT,
  period_start TIMESTAMPTZ,
  period_end TIMESTAMPTZ,
  receipt_id TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cost_events_project
  ON cost_events(project_id, provider, category, created_at DESC);

CREATE TABLE IF NOT EXISTS cost_anomalies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  cost_event_id TEXT REFERENCES cost_events(id),
  decision_id TEXT REFERENCES decisions(id),
  provider TEXT NOT NULL,
  anomaly_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  amount_usd_micros BIGINT NOT NULL,
  threshold_usd_micros BIGINT NOT NULL,
  summary TEXT NOT NULL,
  recommended_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ,
  UNIQUE(project_id, cost_event_id, anomaly_type)
);
CREATE INDEX IF NOT EXISTS idx_cost_anomalies_project
  ON cost_anomalies(project_id, status, severity, created_at DESC);

CREATE TABLE IF NOT EXISTS commitments (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  external_resource_id TEXT REFERENCES external_resources(id),
  provider TEXT NOT NULL,
  commitment_type TEXT NOT NULL,
  amount_usd_micros BIGINT NOT NULL,
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_commitments_project
  ON commitments(project_id, provider, status, period_end);

CREATE TABLE IF NOT EXISTS deployment_targets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  app_name TEXT NOT NULL,
  repo TEXT NOT NULL,
  environment TEXT NOT NULL,
  provider_account_id TEXT REFERENCES provider_accounts(id),
  target_kind TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  domains JSONB NOT NULL DEFAULT '[]'::jsonb,
  required_resources JSONB NOT NULL DEFAULT '[]'::jsonb,
  secret_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  health_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
  rollback_points JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'created',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, app_name, environment)
);
CREATE INDEX IF NOT EXISTS idx_deployment_targets_project
  ON deployment_targets(project_id, app_name, environment, status);

CREATE TABLE IF NOT EXISTS deployment_environment_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  environment TEXT NOT NULL,
  runtime_mode TEXT NOT NULL DEFAULT 'metadata_only',
  allowed_target_kinds JSONB NOT NULL DEFAULT '[]'::jsonb,
  requires_change_set BOOLEAN NOT NULL DEFAULT true,
  requires_budget_reservation BOOLEAN NOT NULL DEFAULT true,
  requires_approval BOOLEAN NOT NULL DEFAULT true,
  requires_health_check BOOLEAN NOT NULL DEFAULT true,
  requires_rollback_plan BOOLEAN NOT NULL DEFAULT true,
  rollout_notes TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, environment)
);
CREATE INDEX IF NOT EXISTS idx_deployment_environment_policies_project
  ON deployment_environment_policies(project_id, environment, status);

CREATE TABLE IF NOT EXISTS changesets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  risk_tier INTEGER NOT NULL DEFAULT 2,
  operations JSONB NOT NULL DEFAULT '[]'::jsonb,
  expected_cost_usd_micros BIGINT NOT NULL DEFAULT 0,
  cost_quote JSONB NOT NULL DEFAULT '{}'::jsonb,
  reservation_id TEXT,
  approval_decision_id TEXT REFERENCES decisions(id),
  rollback_plan JSONB NOT NULL DEFAULT '[]'::jsonb,
  expected_receipts JSONB NOT NULL DEFAULT '[]'::jsonb,
  policy_result JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  applied_by TEXT,
  applied_at TIMESTAMPTZ,
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_changesets_project
  ON changesets(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS deployment_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  deployment_target_id TEXT NOT NULL REFERENCES deployment_targets(id),
  changeset_id TEXT REFERENCES changesets(id),
  decision_id TEXT REFERENCES decisions(id),
  release_artifact_id TEXT REFERENCES artifacts(id),
  health_artifact_id TEXT REFERENCES artifacts(id),
  rollback_artifact_id TEXT REFERENCES artifacts(id),
  app_name TEXT NOT NULL,
  environment TEXT NOT NULL,
  repo TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  commit_ref TEXT,
  status TEXT NOT NULL DEFAULT 'created',
  release_packet JSONB NOT NULL DEFAULT '{}'::jsonb,
  health_check_results JSONB NOT NULL DEFAULT '[]'::jsonb,
  rollback_plan JSONB NOT NULL DEFAULT '[]'::jsonb,
  release_receipt_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployment_runs_project
  ON deployment_runs(project_id, status, created_at DESC);
ALTER TABLE deployment_runs ADD COLUMN IF NOT EXISTS decision_id TEXT REFERENCES decisions(id);

CREATE TABLE IF NOT EXISTS changeset_operations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  changeset_id TEXT NOT NULL REFERENCES changesets(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  operation_index INTEGER NOT NULL,
  skill_id TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_ref TEXT,
  desired_state JSONB NOT NULL DEFAULT '{}'::jsonb,
  risk_tier INTEGER NOT NULL DEFAULT 2,
  destructive BOOLEAN NOT NULL DEFAULT false,
  production_affecting BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'pending',
  resource_receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(changeset_id, operation_index)
);

CREATE TABLE IF NOT EXISTS resource_receipts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  changeset_id TEXT REFERENCES changesets(id),
  operation_id TEXT REFERENCES changeset_operations(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  receipt_id TEXT REFERENCES receipts(id),
  dedupe_key TEXT NOT NULL,
  provider TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_ref TEXT,
  status TEXT NOT NULL DEFAULT 'in_progress',
  provider_ref TEXT,
  request_hash TEXT NOT NULL,
  result JSONB,
  error JSONB,
  created_by_job_id TEXT REFERENCES jobs(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_resource_receipts_project
  ON resource_receipts(project_id, provider, status, created_at DESC);

CREATE TABLE IF NOT EXISTS provider_health (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  provider TEXT NOT NULL,
  status TEXT NOT NULL,
  api_available BOOLEAN NOT NULL DEFAULT false,
  rate_limit_status TEXT NOT NULL DEFAULT 'unknown',
  usage_cap_status TEXT NOT NULL DEFAULT 'unknown',
  last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  detail JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_provider_health_project
  ON provider_health(project_id, provider, last_checked_at DESC);

CREATE TABLE IF NOT EXISTS worker_pools (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  pool_key TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'local',
  provider_account_id TEXT REFERENCES provider_accounts(id),
  provider TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  current_workers INTEGER NOT NULL DEFAULT 0,
  desired_workers INTEGER NOT NULL DEFAULT 0,
  min_workers INTEGER NOT NULL DEFAULT 0,
  max_workers INTEGER NOT NULL DEFAULT 1,
  max_queue_depth_per_worker INTEGER NOT NULL DEFAULT 5,
  cost_per_worker_hour_usd_micros BIGINT NOT NULL DEFAULT 0,
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, pool_key)
);
CREATE INDEX IF NOT EXISTS idx_worker_pools_project
  ON worker_pools(project_id, status, kind);

CREATE TABLE IF NOT EXISTS scale_plans (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  worker_pool_id TEXT REFERENCES worker_pools(id),
  status TEXT NOT NULL,
  trigger_reason TEXT NOT NULL,
  queue_depth INTEGER NOT NULL DEFAULT 0,
  running_jobs INTEGER NOT NULL DEFAULT 0,
  leased_jobs INTEGER NOT NULL DEFAULT 0,
  retryable_jobs INTEGER NOT NULL DEFAULT 0,
  current_workers INTEGER NOT NULL DEFAULT 0,
  desired_workers_before INTEGER NOT NULL DEFAULT 0,
  desired_workers_after INTEGER NOT NULL DEFAULT 0,
  target_workers INTEGER NOT NULL DEFAULT 0,
  budget_state TEXT NOT NULL,
  budget_available_usd_micros BIGINT NOT NULL DEFAULT 0,
  estimated_hourly_delta_usd_micros BIGINT NOT NULL DEFAULT 0,
  provider_health_status TEXT,
  actions JSONB NOT NULL DEFAULT '[]'::jsonb,
  recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
  changeset_id TEXT REFERENCES changesets(id),
  decision_id TEXT REFERENCES decisions(id),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_scale_plans_project
  ON scale_plans(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS drift_reports (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  classification TEXT NOT NULL,
  summary TEXT NOT NULL,
  unmanaged_count INTEGER NOT NULL DEFAULT 0,
  orphaned_count INTEGER NOT NULL DEFAULT 0,
  security_impacting_count INTEGER NOT NULL DEFAULT 0,
  production_impacting_count INTEGER NOT NULL DEFAULT 0,
  recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_drift_reports_project
  ON drift_reports(project_id, classification, created_at DESC);

CREATE TABLE IF NOT EXISTS resource_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  policy_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  body JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, policy_key)
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider_account_id TEXT REFERENCES provider_accounts(id),
  provider TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  resources_seen INTEGER NOT NULL DEFAULT 0,
  resources_changed INTEGER NOT NULL DEFAULT 0,
  costs_seen INTEGER NOT NULL DEFAULT 0,
  started_by TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  error JSONB,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_project
  ON reconciliation_runs(project_id, started_at DESC);

CREATE TABLE IF NOT EXISTS budget_policies (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  daily_cap_usd_micros BIGINT NOT NULL,
  monthly_cap_usd_micros BIGINT NOT NULL,
  burst_cap_usd_micros_per_min BIGINT NOT NULL,
  max_concurrent_jobs INTEGER NOT NULL DEFAULT 1,
  max_workers_total INTEGER NOT NULL DEFAULT 1,
  allowed_model_tiers JSONB NOT NULL DEFAULT '["gpt-5.2-codex"]'::jsonb,
  soft_cap_pct NUMERIC NOT NULL DEFAULT 0.7,
  auto_lite_mode_pct NUMERIC NOT NULL DEFAULT 0.85,
  freeze_provisioning_pct NUMERIC NOT NULL DEFAULT 0.9,
  freeze_tier2_pct NUMERIC NOT NULL DEFAULT 0.95,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS budget_allocations (
  id TEXT PRIMARY KEY,
  parent_allocation_id TEXT REFERENCES budget_allocations(id),
  project_id TEXT NOT NULL REFERENCES projects(id),
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  amount_usd_micros BIGINT NOT NULL,
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE budget_allocations ADD COLUMN IF NOT EXISTS project_id TEXT REFERENCES projects(id);
CREATE INDEX IF NOT EXISTS idx_budget_allocations_project
  ON budget_allocations(project_id, status, period_end);

CREATE TABLE IF NOT EXISTS budget_reservations (
  id TEXT PRIMARY KEY,
  allocation_id TEXT REFERENCES budget_allocations(id),
  project_id TEXT NOT NULL REFERENCES projects(id),
  job_id TEXT REFERENCES jobs(id),
  category TEXT NOT NULL,
  amount_usd_micros BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  reason TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_active ON budget_reservations(project_id, status, expires_at);

CREATE TABLE IF NOT EXISTS cost_receipts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  reservation_id TEXT REFERENCES budget_reservations(id),
  receipt_id TEXT REFERENCES receipts(id),
  category TEXT NOT NULL,
  skill_or_model_id TEXT NOT NULL,
  estimated_usd_micros BIGINT NOT NULL,
  actual_usd_micros BIGINT,
  status TEXT NOT NULL DEFAULT 'estimated',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox_messages (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  channel TEXT NOT NULL,
  subject TEXT,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  dedupe_key TEXT NOT NULL,
  receipt_id TEXT REFERENCES receipts(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS data_boundary TEXT NOT NULL DEFAULT 'internal';

CREATE TABLE IF NOT EXISTS comms_channel_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider TEXT NOT NULL,
  delivery_mode TEXT NOT NULL DEFAULT 'draft_only',
  allowed_data_boundaries JSONB NOT NULL DEFAULT '["public", "internal"]'::jsonb,
  rollout_notes TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_comms_channel_policies_project
  ON comms_channel_policies(project_id, provider, status);

CREATE TABLE IF NOT EXISTS inbox_messages (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  channel TEXT NOT NULL,
  raw_ref TEXT,
  subject TEXT,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'received',
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  parsed JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE inbox_messages ADD COLUMN IF NOT EXISTS data_boundary TEXT NOT NULL DEFAULT 'internal';

CREATE TABLE IF NOT EXISTS deadletters (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'open',
  incident_task_id TEXT,
  incident_artifact_id TEXT,
  remediation_decision_id TEXT,
  requeued_job_id TEXT,
  resolved_by TEXT,
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE deadletters ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open';
ALTER TABLE deadletters ADD COLUMN IF NOT EXISTS incident_task_id TEXT;
ALTER TABLE deadletters ADD COLUMN IF NOT EXISTS incident_artifact_id TEXT;
ALTER TABLE deadletters ADD COLUMN IF NOT EXISTS remediation_decision_id TEXT;
ALTER TABLE deadletters ADD COLUMN IF NOT EXISTS requeued_job_id TEXT;
ALTER TABLE deadletters ADD COLUMN IF NOT EXISTS resolved_by TEXT;
ALTER TABLE deadletters ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE deadletters ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  data JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_project_created ON events(project_id, created_at DESC);
