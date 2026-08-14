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
ALTER TABLE pack_installs ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE pack_installs ADD COLUMN IF NOT EXISTS yaml_blob TEXT;

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
  -- Receipts are declared after jobs to preserve the core job->receipt FK order.
  -- Account Connect stores this audit reference without a forward FK so clean
  -- databases can bootstrap deterministically.
  receipt_id TEXT,
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
  credential_secret_ref_id TEXT,
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
ALTER TABLE entitlement_sources ADD COLUMN IF NOT EXISTS credential_secret_ref_id TEXT;

CREATE TABLE IF NOT EXISTS entitlement_connection_sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  auth_method TEXT NOT NULL,
  connection_mode TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'operator_action_required',
  requested_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  authorization_url TEXT,
  device_code TEXT,
  user_code TEXT,
  verification_uri TEXT,
  login_command TEXT,
  connection_ref TEXT,
  instructions JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  requested_by TEXT NOT NULL DEFAULT 'system',
  completed_by TEXT,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_entitlement_connection_sessions_project_created
  ON entitlement_connection_sessions(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entitlement_connection_sessions_source_status
  ON entitlement_connection_sessions(project_id, source_id, status);

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

CREATE TABLE IF NOT EXISTS kpis (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  kpi_key TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  owner_role_id TEXT REFERENCES roles(id),
  target_value NUMERIC NOT NULL,
  current_value NUMERIC NOT NULL DEFAULT 0,
  unit TEXT NOT NULL DEFAULT 'count',
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, kpi_key)
);
CREATE INDEX IF NOT EXISTS idx_kpis_project_status
  ON kpis(project_id, status, kpi_key);

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

-- Work Orchestration Plane (BOSS-270..278): one explainable portfolio,
-- quota ledger, external work-tracker binding, and delivery-presence surface.
-- Plane is the default work tracker; the durable BOSS jobs table below remains
-- the execution authority for leases, retries, deadletters, and receipts.
CREATE TABLE IF NOT EXISTS work_items (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  priority_band TEXT NOT NULL DEFAULT 'p2',
  effective_score NUMERIC(7, 2) NOT NULL DEFAULT 50,
  strategic_value INTEGER NOT NULL DEFAULT 50,
  customer_impact INTEGER NOT NULL DEFAULT 50,
  urgency INTEGER NOT NULL DEFAULT 50,
  cost_of_delay INTEGER NOT NULL DEFAULT 50,
  risk_reduction INTEGER NOT NULL DEFAULT 50,
  dependency_pressure INTEGER NOT NULL DEFAULT 50,
  confidence INTEGER NOT NULL DEFAULT 75,
  manual_adjustment INTEGER NOT NULL DEFAULT 0,
  rationale TEXT NOT NULL DEFAULT '',
  workload_class TEXT NOT NULL DEFAULT 'general',
  estimated_units INTEGER NOT NULL DEFAULT 1,
  allocated_units INTEGER NOT NULL DEFAULT 0,
  allocation_status TEXT NOT NULL DEFAULT 'unallocated',
  due_at TIMESTAMPTZ,
  owner_actor TEXT,
  evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  source TEXT NOT NULL DEFAULT 'boss',
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, subject_type, subject_id),
  CHECK (priority_band IN ('p0', 'p1', 'p2', 'p3', 'p4')),
  CHECK (strategic_value BETWEEN 0 AND 100),
  CHECK (customer_impact BETWEEN 0 AND 100),
  CHECK (urgency BETWEEN 0 AND 100),
  CHECK (cost_of_delay BETWEEN 0 AND 100),
  CHECK (risk_reduction BETWEEN 0 AND 100),
  CHECK (dependency_pressure BETWEEN 0 AND 100),
  CHECK (confidence BETWEEN 0 AND 100),
  CHECK (manual_adjustment BETWEEN -100 AND 100),
  CHECK (estimated_units > 0),
  CHECK (allocated_units >= 0)
);
CREATE INDEX IF NOT EXISTS idx_work_items_ranked
  ON work_items(project_id, status, effective_score DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_workload
  ON work_items(project_id, workload_class, allocation_status);

CREATE TABLE IF NOT EXISTS resource_quotas (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  reserved_min INTEGER NOT NULL DEFAULT 0,
  soft_limit INTEGER NOT NULL DEFAULT 1,
  hard_limit INTEGER NOT NULL DEFAULT 1,
  burst_limit INTEGER NOT NULL DEFAULT 0,
  allocation_weight NUMERIC(8, 3) NOT NULL DEFAULT 1,
  borrow_enabled BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'active',
  policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, scope_type, scope_id, resource_kind),
  CHECK (scope_type IN ('global', 'org', 'workspace', 'project', 'agent', 'workload')),
  CHECK (reserved_min >= 0),
  CHECK (soft_limit >= reserved_min),
  CHECK (hard_limit >= soft_limit),
  CHECK (burst_limit >= 0),
  CHECK (allocation_weight > 0)
);
CREATE INDEX IF NOT EXISTS idx_resource_quotas_project
  ON resource_quotas(project_id, resource_kind, status);

CREATE TABLE IF NOT EXISTS resource_allocations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  quota_id TEXT NOT NULL REFERENCES resource_quotas(id) ON DELETE CASCADE,
  resource_kind TEXT NOT NULL,
  requested_units INTEGER NOT NULL,
  granted_units INTEGER NOT NULL DEFAULT 0,
  used_units INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'planned',
  reason TEXT NOT NULL DEFAULT '',
  score_snapshot NUMERIC(7, 2) NOT NULL,
  policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'allocator',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ,
  released_at TIMESTAMPTZ,
  CHECK (requested_units > 0),
  CHECK (granted_units >= 0),
  CHECK (used_units >= 0),
  CHECK (used_units <= granted_units)
);
CREATE INDEX IF NOT EXISTS idx_resource_allocations_work
  ON resource_allocations(project_id, work_item_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_resource_allocations_quota
  ON resource_allocations(quota_id, status, expires_at);

CREATE TABLE IF NOT EXISTS work_tracker_providers (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  adapter_kind TEXT NOT NULL DEFAULT 'plugin',
  status TEXT NOT NULL DEFAULT 'active',
  is_default BOOLEAN NOT NULL DEFAULT false,
  enforcement_mode TEXT NOT NULL DEFAULT 'required',
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, provider_key),
  CHECK (enforcement_mode IN ('required', 'advisory', 'disabled'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_work_tracker_default
  ON work_tracker_providers(project_id)
  WHERE is_default = true AND status = 'active';

CREATE TABLE IF NOT EXISTS work_tracker_bindings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider_id TEXT NOT NULL REFERENCES work_tracker_providers(id),
  work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  external_workspace_id TEXT,
  external_project_id TEXT,
  external_issue_id TEXT,
  external_key TEXT,
  external_url TEXT,
  external_state TEXT,
  sync_status TEXT NOT NULL DEFAULT 'pending_create',
  sync_error JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_synced_at TIMESTAMPTZ,
  UNIQUE(project_id, provider_id, work_item_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_work_tracker_external_issue
  ON work_tracker_bindings(project_id, provider_id, external_issue_id)
  WHERE external_issue_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_tracker_bindings_status
  ON work_tracker_bindings(project_id, sync_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS delivery_bot_identities (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  -- Agent Studio and secret_refs are declared later in this bootstrap file.
  -- Their foreign keys are attached after those tables exist.
  agent_definition_id TEXT,
  agent_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  external_identity TEXT,
  credential_secret_ref_id TEXT,
  avatar_url TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, provider, agent_key)
);

CREATE TABLE IF NOT EXISTS delivery_status_channels (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  channel_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  purpose TEXT NOT NULL,
  external_channel_id TEXT,
  external_url TEXT,
  bot_identity_id TEXT REFERENCES delivery_bot_identities(id),
  event_kinds JSONB NOT NULL DEFAULT '[]'::jsonb,
  allowed_data_boundaries JSONB NOT NULL DEFAULT '["public", "internal"]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, provider, channel_key)
);

CREATE TABLE IF NOT EXISTS delivery_presence_events (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  channel_id TEXT REFERENCES delivery_status_channels(id),
  bot_identity_id TEXT REFERENCES delivery_bot_identities(id),
  provider TEXT NOT NULL,
  event_kind TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  external_url TEXT,
  receipt_id TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_delivery_presence_project
  ON delivery_presence_events(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  work_item_id TEXT REFERENCES work_items(id),
  tracker_binding_id TEXT REFERENCES work_tracker_bindings(id),
  allocation_id TEXT REFERENCES resource_allocations(id),
  kind TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  priority_score NUMERIC(7, 2),
  workload_class TEXT NOT NULL DEFAULT 'general',
  resource_units INTEGER NOT NULL DEFAULT 1,
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
  dedupe_key TEXT,
  run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dedupe_key TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS work_item_id TEXT REFERENCES work_items(id);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tracker_binding_id TEXT REFERENCES work_tracker_bindings(id);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS allocation_id TEXT REFERENCES resource_allocations(id);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority_score NUMERIC(7, 2);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workload_class TEXT NOT NULL DEFAULT 'general';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS resource_units INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs (status, run_after, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_ranked_claimable
  ON jobs (status, run_after, priority_score DESC, priority, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_project_dedupe
  ON jobs(project_id, dedupe_key)
  WHERE dedupe_key IS NOT NULL;

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
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'delivery_presence_events_receipt_id_fkey'
  ) THEN
    ALTER TABLE delivery_presence_events
      ADD CONSTRAINT delivery_presence_events_receipt_id_fkey
      FOREIGN KEY (receipt_id) REFERENCES receipts(id);
  END IF;
END $$;
-- account_connect_auth_sessions.receipt_id is declared before receipts exists;
-- attach the foreign key now that receipts is defined (idempotent guard).
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'account_connect_auth_sessions_receipt_id_fkey'
  ) THEN
    ALTER TABLE account_connect_auth_sessions
      ADD CONSTRAINT account_connect_auth_sessions_receipt_id_fkey
      FOREIGN KEY (receipt_id) REFERENCES receipts(id);
  END IF;
END $$;
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
  scope_type TEXT NOT NULL DEFAULT 'project',
  scope_id TEXT NOT NULL,
  ref_kind TEXT NOT NULL DEFAULT 'env',
  ref_key TEXT NOT NULL,
  provider TEXT,
  secret_kind TEXT NOT NULL DEFAULT 'api_token',
  storage_backend TEXT NOT NULL DEFAULT 'environment',
  status TEXT NOT NULL DEFAULT 'active',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  rotation_due_at TIMESTAMPTZ,
  last_verified_at TIMESTAMPTZ,
  supersedes_secret_ref_id TEXT REFERENCES secret_refs(id),
  overlap_expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  revoked_by TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, ref_kind, ref_key)
);
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'project';
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS scope_id TEXT;
UPDATE secret_refs SET scope_id = project_id WHERE scope_id IS NULL;
ALTER TABLE secret_refs ALTER COLUMN scope_id SET NOT NULL;
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS secret_kind TEXT NOT NULL DEFAULT 'api_token';
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS storage_backend TEXT NOT NULL DEFAULT 'environment';
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS rotation_due_at TIMESTAMPTZ;
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS supersedes_secret_ref_id TEXT REFERENCES secret_refs(id);
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS overlap_expires_at TIMESTAMPTZ;
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE secret_refs ADD COLUMN IF NOT EXISTS revoked_by TEXT;
CREATE INDEX IF NOT EXISTS idx_secret_refs_project
  ON secret_refs(project_id, provider, status);
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'entitlement_sources_credential_secret_ref_id_fkey'
  ) THEN
    ALTER TABLE entitlement_sources
      ADD CONSTRAINT entitlement_sources_credential_secret_ref_id_fkey
      FOREIGN KEY (credential_secret_ref_id) REFERENCES secret_refs(id) ON DELETE SET NULL;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'delivery_bot_identities_credential_secret_ref_id_fkey'
  ) THEN
    ALTER TABLE delivery_bot_identities
      ADD CONSTRAINT delivery_bot_identities_credential_secret_ref_id_fkey
      FOREIGN KEY (credential_secret_ref_id) REFERENCES secret_refs(id);
  END IF;
END $$;

-- Project-scoped GitHub connectivity. Credential material remains outside BOSS;
-- github_connections stores only a SecretRef pointer and non-secret installation
-- identity. Repository bindings are the canonical target boundary consumed by
-- future branch-policy, sync, lease, and merge-orchestration slices.
CREATE TABLE IF NOT EXISTS github_connections (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  connection_label TEXT NOT NULL,
  auth_method TEXT NOT NULL,
  credential_ref TEXT NOT NULL,
  app_id TEXT,
  installation_id TEXT,
  api_base_url TEXT NOT NULL DEFAULT 'https://api.github.com',
  status TEXT NOT NULL DEFAULT 'configured',
  required_permissions JSONB NOT NULL DEFAULT '["pull"]'::jsonb,
  granted_permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  missing_permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  account_login TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_test_status TEXT,
  last_tested_at TIMESTAMPTZ,
  last_error JSONB,
  disabled_at TIMESTAMPTZ,
  disabled_by TEXT,
  revoked_at TIMESTAMPTZ,
  revoked_by TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, connection_label),
  CHECK (auth_method IN ('github_app', 'personal_access_token')),
  CHECK (status IN ('configured', 'active', 'error', 'disabled', 'revoked'))
);
CREATE INDEX IF NOT EXISTS idx_github_connections_project
  ON github_connections(project_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_github_connections_project_label_ci
  ON github_connections(project_id, lower(connection_label));

CREATE TABLE IF NOT EXISTS github_repository_bindings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  connection_id TEXT NOT NULL REFERENCES github_connections(id) ON DELETE CASCADE,
  repository_id BIGINT,
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  full_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'primary',
  default_branch TEXT,
  visibility TEXT,
  archived BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'active',
  permissions JSONB NOT NULL DEFAULT '{}'::jsonb,
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_synced_at TIMESTAMPTZ,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, full_name),
  CHECK (role IN ('primary', 'source', 'upstream', 'mirror')),
  CHECK (status IN ('active', 'disabled'))
);
CREATE INDEX IF NOT EXISTS idx_github_repository_bindings_project
  ON github_repository_bindings(project_id, status, role, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_github_repository_bindings_connection
  ON github_repository_bindings(connection_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_github_repository_bindings_project_name_ci
  ON github_repository_bindings(project_id, lower(full_name));
CREATE UNIQUE INDEX IF NOT EXISTS uq_github_repository_binding_primary
  ON github_repository_bindings(project_id)
  WHERE role = 'primary' AND status = 'active';

CREATE TABLE IF NOT EXISTS github_branch_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  supersedes_policy_id TEXT REFERENCES github_branch_policies(id),
  status TEXT NOT NULL DEFAULT 'active',
  protected_ref_patterns JSONB NOT NULL DEFAULT '["main", "master", "release/*"]'::jsonb,
  allowed_branch_prefixes JSONB NOT NULL DEFAULT '["ai/", "feat/", "fix/", "chore/", "docs/", "release/"]'::jsonb,
  branch_name_pattern TEXT NOT NULL DEFAULT '^[A-Za-z0-9._/-]+$',
  allowed_update_methods JSONB NOT NULL DEFAULT '["fast_forward", "merge_commit", "rebase"]'::jsonb,
  allowed_merge_methods JSONB NOT NULL DEFAULT '["merge", "squash", "rebase"]'::jsonb,
  required_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
  required_approvals INTEGER NOT NULL DEFAULT 1,
  require_pull_request BOOLEAN NOT NULL DEFAULT true,
  require_linear_history BOOLEAN NOT NULL DEFAULT false,
  require_signed_commits BOOLEAN NOT NULL DEFAULT false,
  automation_updates_enabled BOOLEAN NOT NULL DEFAULT true,
  force_push_allowed BOOLEAN NOT NULL DEFAULT false,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(repository_binding_id, version),
  CHECK (version > 0),
  CHECK (required_approvals >= 0),
  CHECK (status IN ('active', 'superseded', 'disabled'))
);
CREATE INDEX IF NOT EXISTS idx_github_branch_policies_project
  ON github_branch_policies(project_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_github_branch_policy_active
  ON github_branch_policies(repository_binding_id)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS github_branch_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  branch_policy_id TEXT REFERENCES github_branch_policies(id),
  branch_policy_version INTEGER,
  source_ref TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  source_commit_sha TEXT,
  source_tree_sha TEXT,
  target_commit_sha TEXT,
  target_tree_sha TEXT,
  merge_base_commit_sha TEXT,
  merge_base_tree_sha TEXT,
  ahead_by INTEGER NOT NULL DEFAULT 0,
  behind_by INTEGER NOT NULL DEFAULT 0,
  topology_classification TEXT NOT NULL,
  classification TEXT NOT NULL,
  refs JSONB NOT NULL DEFAULT '{}'::jsonb,
  comparison JSONB NOT NULL DEFAULT '{}'::jsonb,
  protection JSONB NOT NULL DEFAULT '{}'::jsonb,
  checks JSONB NOT NULL DEFAULT '[]'::jsonb,
  policy_evaluation JSONB NOT NULL DEFAULT '{}'::jsonb,
  receipt_id TEXT NOT NULL REFERENCES receipts(id),
  dedupe_key TEXT NOT NULL,
  observed_by TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key),
  CHECK (ahead_by >= 0),
  CHECK (behind_by >= 0),
  CHECK (topology_classification IN (
    'equal', 'tree_equivalent_rewrite', 'fast_forward', 'behind',
    'diverged', 'missing'
  )),
  CHECK (classification IN (
    'equal', 'tree_equivalent_rewrite', 'fast_forward', 'behind',
    'diverged', 'missing', 'protected_blocked'
  ))
);
CREATE INDEX IF NOT EXISTS idx_github_branch_snapshots_binding
  ON github_branch_snapshots(
    repository_binding_id, observed_at DESC, source_ref, target_ref
  );

CREATE TABLE IF NOT EXISTS github_branch_leases (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  ref_name TEXT NOT NULL,
  owner_kind TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  run_id TEXT,
  worktree_path TEXT,
  acquire_key TEXT NOT NULL,
  lease_token_hash TEXT NOT NULL,
  fencing_token BIGSERIAL NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  acquired_by TEXT NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_expires_at TIMESTAMPTZ NOT NULL,
  released_by TEXT,
  released_at TIMESTAMPTZ,
  release_reason TEXT,
  reclaimed_from_lease_id TEXT REFERENCES github_branch_leases(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, acquire_key),
  CHECK (status IN ('active', 'released', 'expired', 'reclaimed')),
  CHECK (owner_kind IN (
    'task', 'workflow_run', 'code_task_run', 'agent', 'manual'
  ))
);
CREATE INDEX IF NOT EXISTS idx_github_branch_leases_binding
  ON github_branch_leases(
    repository_binding_id, ref_name, status, lease_expires_at DESC
  );
CREATE UNIQUE INDEX IF NOT EXISTS uq_github_branch_lease_active
  ON github_branch_leases(repository_binding_id, ref_name)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS github_code_sync_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  branch_policy_id TEXT NOT NULL REFERENCES github_branch_policies(id),
  branch_policy_version INTEGER NOT NULL,
  initial_snapshot_id TEXT NOT NULL REFERENCES github_branch_snapshots(id),
  result_snapshot_id TEXT REFERENCES github_branch_snapshots(id),
  branch_lease_id TEXT REFERENCES github_branch_leases(id),
  fencing_token BIGINT,
  source_ref TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  topology_classification TEXT NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  changeset_id TEXT,
  conflict_set_id TEXT,
  decision_id TEXT REFERENCES decisions(id),
  resolution_branch TEXT,
  rollback_ref TEXT,
  rollback_sha TEXT,
  apply_attempts INTEGER NOT NULL DEFAULT 0,
  receipt_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  resource_receipt_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  error JSONB,
  created_by TEXT NOT NULL,
  approved_by TEXT,
  applied_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  UNIQUE(project_id, dedupe_key),
  CHECK (action IN (
    'noop', 'preserve_remote_identity', 'fast_forward_target',
    'fast_forward_source', 'create_resolution_branch', 'blocked'
  )),
  CHECK (status IN (
    'planned', 'pending_approval', 'approved', 'applying', 'succeeded',
    'noop', 'conflicted', 'blocked', 'failed', 'deadlettered'
  ))
);
CREATE INDEX IF NOT EXISTS idx_github_code_sync_runs_project
  ON github_code_sync_runs(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_github_code_sync_runs_binding
  ON github_code_sync_runs(repository_binding_id, source_ref, target_ref, created_at DESC);

CREATE TABLE IF NOT EXISTS github_conflict_sets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  code_sync_run_id TEXT NOT NULL REFERENCES github_code_sync_runs(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  resolution_branch TEXT NOT NULL,
  resolution_branch_head_sha TEXT NOT NULL,
  base_commit_sha TEXT,
  base_tree_sha TEXT,
  ours_commit_sha TEXT NOT NULL,
  ours_tree_sha TEXT NOT NULL,
  theirs_commit_sha TEXT NOT NULL,
  theirs_tree_sha TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  files JSONB NOT NULL DEFAULT '[]'::jsonb,
  conflict_count INTEGER NOT NULL DEFAULT 0,
  decision_id TEXT REFERENCES decisions(id),
  resolved_by_plan_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ,
  CHECK (status IN (
    'open', 'resolution_proposed', 'approved', 'resolved', 'blocked'
  )),
  CHECK (conflict_count >= 0)
);
CREATE INDEX IF NOT EXISTS idx_github_conflict_sets_project
  ON github_conflict_sets(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS github_resolution_plans (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  conflict_set_id TEXT NOT NULL REFERENCES github_conflict_sets(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed',
  resolved_files JSONB NOT NULL DEFAULT '[]'::jsonb,
  validation_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  independent_review JSONB NOT NULL DEFAULT '{}'::jsonb,
  decision_id TEXT REFERENCES decisions(id),
  changeset_id TEXT,
  result_commit_sha TEXT,
  created_by TEXT NOT NULL,
  approved_by TEXT,
  applied_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at TIMESTAMPTZ,
  UNIQUE(conflict_set_id, version),
  CHECK (version > 0),
  CHECK (status IN ('proposed', 'approved', 'applied', 'rejected', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_github_resolution_plans_project
  ON github_resolution_plans(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS github_merge_authorizations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  branch_policy_id TEXT NOT NULL REFERENCES github_branch_policies(id),
  branch_policy_version INTEGER NOT NULL,
  code_sync_run_id TEXT REFERENCES github_code_sync_runs(id),
  resolution_plan_id TEXT REFERENCES github_resolution_plans(id),
  pull_request_number BIGINT NOT NULL,
  pull_request_url TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  head_commit_sha TEXT NOT NULL,
  head_tree_sha TEXT NOT NULL,
  base_commit_sha TEXT NOT NULL,
  base_tree_sha TEXT NOT NULL,
  merge_method TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_approval',
  dedupe_key TEXT NOT NULL,
  required_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
  observed_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
  required_approvals INTEGER NOT NULL DEFAULT 1,
  observed_reviews JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence_bundle JSONB NOT NULL DEFAULT '{}'::jsonb,
  policy_evaluation JSONB NOT NULL DEFAULT '{}'::jsonb,
  authorized_by TEXT,
  authorized_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  queue_position BIGSERIAL NOT NULL UNIQUE,
  queued_at TIMESTAMPTZ,
  branch_lease_id TEXT REFERENCES github_branch_leases(id),
  fencing_token BIGINT,
  changeset_id TEXT,
  apply_attempts INTEGER NOT NULL DEFAULT 0,
  receipt_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  resource_receipt_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  result_merge_sha TEXT,
  result_target_tree_sha TEXT,
  rollback_plan JSONB NOT NULL DEFAULT '{}'::jsonb,
  error JSONB,
  created_by TEXT NOT NULL,
  applied_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  merged_at TIMESTAMPTZ,
  UNIQUE(project_id, dedupe_key),
  CHECK (pull_request_number > 0),
  CHECK (required_approvals >= 0),
  CHECK (merge_method IN ('merge', 'squash', 'rebase')),
  CHECK (status IN (
    'pending_approval', 'authorized', 'queued', 'applying', 'merged',
    'blocked', 'expired', 'failed', 'deadlettered', 'cancelled'
  ))
);
CREATE INDEX IF NOT EXISTS idx_github_merge_authorizations_project
  ON github_merge_authorizations(project_id, status, queue_position);
CREATE INDEX IF NOT EXISTS idx_github_merge_authorizations_target_queue
  ON github_merge_authorizations(
    repository_binding_id, target_ref, status, queue_position
  );
CREATE UNIQUE INDEX IF NOT EXISTS uq_github_merge_authorizations_active_pr
  ON github_merge_authorizations(repository_binding_id, pull_request_number)
  WHERE status IN (
    'pending_approval', 'authorized', 'queued', 'applying'
  );

CREATE TABLE IF NOT EXISTS github_reconciliation_signals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  -- Inbound webhooks are declared later in this bootstrap file.
  inbound_webhook_id TEXT,
  source TEXT NOT NULL,
  delivery_id TEXT,
  provider_event TEXT,
  action TEXT,
  ref_name TEXT NOT NULL,
  expected_commit_sha TEXT,
  expected_deleted BOOLEAN NOT NULL DEFAULT false,
  payload_hash TEXT,
  dedupe_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  reconciliation_run_id TEXT,
  observed_commit_sha TEXT,
  error JSONB,
  provider_occurred_at TIMESTAMPTZ,
  processed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key),
  CHECK (source IN ('webhook', 'poll', 'repair')),
  CHECK (status IN (
    'pending', 'processing', 'applied', 'stale', 'failed', 'deadlettered'
  ))
);
CREATE INDEX IF NOT EXISTS idx_github_reconciliation_signals_binding
  ON github_reconciliation_signals(
    repository_binding_id, ref_name, status, created_at DESC
  );

CREATE TABLE IF NOT EXISTS github_reconciliation_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  trigger_signal_id TEXT REFERENCES github_reconciliation_signals(id) ON DELETE SET NULL,
  repair_of_run_id TEXT REFERENCES github_reconciliation_runs(id) ON DELETE SET NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  dedupe_key TEXT NOT NULL,
  requested_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
  observation_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  receipt_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  rate_limit_status JSONB NOT NULL DEFAULT '{}'::jsonb,
  next_attempt_at TIMESTAMPTZ,
  deadletter_id TEXT,
  error JSONB,
  created_by TEXT NOT NULL,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key),
  CHECK (source IN ('webhook', 'poll', 'repair')),
  CHECK (status IN (
    'queued', 'running', 'succeeded', 'failed', 'deadlettered', 'cancelled'
  )),
  CHECK (attempt_count >= 0),
  CHECK (max_attempts BETWEEN 1 AND 10)
);
CREATE INDEX IF NOT EXISTS idx_github_reconciliation_runs_project
  ON github_reconciliation_runs(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_github_reconciliation_runs_binding
  ON github_reconciliation_runs(
    repository_binding_id, status, created_at DESC
  );
ALTER TABLE github_reconciliation_runs
  ADD COLUMN IF NOT EXISTS observation_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_github_reconciliation_signal_run'
  ) THEN
    ALTER TABLE github_reconciliation_signals
      ADD CONSTRAINT fk_github_reconciliation_signal_run
      FOREIGN KEY (reconciliation_run_id)
      REFERENCES github_reconciliation_runs(id) ON DELETE SET NULL;
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS github_repository_observations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  reconciliation_run_id TEXT NOT NULL REFERENCES github_reconciliation_runs(id) ON DELETE CASCADE,
  signal_id TEXT REFERENCES github_reconciliation_signals(id) ON DELETE SET NULL,
  ref_name TEXT NOT NULL,
  expected_commit_sha TEXT,
  exists BOOLEAN NOT NULL,
  commit_sha TEXT,
  tree_sha TEXT,
  protected BOOLEAN NOT NULL DEFAULT false,
  protection JSONB NOT NULL DEFAULT '{}'::jsonb,
  classification TEXT NOT NULL,
  receipt_id TEXT NOT NULL REFERENCES receipts(id),
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(reconciliation_run_id, ref_name),
  CHECK (classification IN (
    'current', 'advanced', 'changed', 'deleted', 'missing',
    'stale_signal', 'unexpected_reappearance'
  ))
);
CREATE INDEX IF NOT EXISTS idx_github_repository_observations_binding
  ON github_repository_observations(
    repository_binding_id, ref_name, observed_at DESC
  );

CREATE TABLE IF NOT EXISTS github_repository_ref_states (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id TEXT NOT NULL REFERENCES github_repository_bindings(id) ON DELETE CASCADE,
  ref_name TEXT NOT NULL,
  exists BOOLEAN NOT NULL,
  commit_sha TEXT,
  tree_sha TEXT,
  protected BOOLEAN NOT NULL DEFAULT false,
  protection JSONB NOT NULL DEFAULT '{}'::jsonb,
  latest_observation_id TEXT NOT NULL REFERENCES github_repository_observations(id) ON DELETE CASCADE,
  latest_signal_id TEXT REFERENCES github_reconciliation_signals(id) ON DELETE SET NULL,
  latest_receipt_id TEXT NOT NULL REFERENCES receipts(id),
  observed_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(repository_binding_id, ref_name)
);
CREATE INDEX IF NOT EXISTS idx_github_repository_ref_states_project
  ON github_repository_ref_states(project_id, repository_binding_id, ref_name);

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
  auth_method TEXT NOT NULL DEFAULT 'api_token',
  credential_ref TEXT,
  credential_refs JSONB NOT NULL DEFAULT '{}'::jsonb,
  connection_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  owner_actor TEXT,
  credential_version INTEGER NOT NULL DEFAULT 1,
  credential_rotated_at TIMESTAMPTZ,
  disabled_at TIMESTAMPTZ,
  disabled_by TEXT,
  revoked_at TIMESTAMPTZ,
  revoked_by TEXT,
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
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS auth_method TEXT NOT NULL DEFAULT 'api_token';
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS credential_refs JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS connection_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS owner_actor TEXT;
UPDATE provider_accounts SET owner_actor = created_by WHERE owner_actor IS NULL;
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS credential_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS credential_rotated_at TIMESTAMPTZ;
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS disabled_by TEXT;
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE provider_accounts ADD COLUMN IF NOT EXISTS revoked_by TEXT;
UPDATE provider_accounts
SET auth_method = CASE provider
  WHEN 'aws' THEN 'access_key'
  WHEN 'azure' THEN 'service_principal'
  WHEN 'gcp' THEN 'service_account'
  WHEN 'ovhcloud' THEN 'application_consumer_key'
  ELSE auth_method
END
WHERE auth_method = 'api_token'
  AND provider IN ('aws', 'azure', 'gcp', 'ovhcloud');
UPDATE provider_accounts
SET credential_refs = CASE provider
  WHEN 'aws' THEN jsonb_build_object(
    'access_key_id', 'env:BOSS_AWS_ACCESS_KEY_ID',
    'secret_access_key', credential_ref
  )
  WHEN 'digitalocean' THEN jsonb_build_object('token', credential_ref)
  WHEN 'azure' THEN jsonb_build_object('client_secret', credential_ref)
  WHEN 'gcp' THEN jsonb_build_object('service_account', credential_ref)
  WHEN 'ovhcloud' THEN jsonb_build_object('application_secret', credential_ref)
  ELSE jsonb_build_object('primary', credential_ref)
END
WHERE credential_refs = '{}'::jsonb
  AND credential_ref IS NOT NULL;
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

-- Durable ProviderOps topology graph (BOSS-240): directed edges between
-- inventory resources (droplet→vpc, volume→droplet, etc.). Inventory + drift
-- remain separate; this table is the minimal edge store for graph views.
CREATE TABLE IF NOT EXISTS resource_topology_edges (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  from_resource_id TEXT NOT NULL REFERENCES external_resources(id) ON DELETE CASCADE,
  to_resource_id TEXT NOT NULL REFERENCES external_resources(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,
  provider TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, from_resource_id, to_resource_id, relation_type),
  CHECK (from_resource_id <> to_resource_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_topology_edges_project
  ON resource_topology_edges(project_id, relation_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_resource_topology_edges_from
  ON resource_topology_edges(project_id, from_resource_id);
CREATE INDEX IF NOT EXISTS idx_resource_topology_edges_to
  ON resource_topology_edges(project_id, to_resource_id);

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
  terraform_export BOOLEAN NOT NULL DEFAULT false,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, app_name, environment)
);
CREATE INDEX IF NOT EXISTS idx_deployment_targets_project
  ON deployment_targets(project_id, app_name, environment, status);
-- Track 1 Terraform export is opt-in per deployment target (default OFF): the BOSS DB
-- stays canonical and .tf is only ever a downstream projection (spec 05 s3.1).
ALTER TABLE deployment_targets
  ADD COLUMN IF NOT EXISTS terraform_export BOOLEAN NOT NULL DEFAULT false;

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

-- Whether a merge to this target's repo deploys by itself, and whether that is currently
-- paused. Defaults to FALSE so that shipping this feature does not silently turn every
-- existing target into one that deploys on merge — enabling it stays a deliberate act.
ALTER TABLE deployment_targets ADD COLUMN IF NOT EXISTS auto_deploy BOOLEAN NOT NULL DEFAULT FALSE;
-- Separate from auto_deploy on purpose: during an incident an operator wants automation to stop
-- WITHOUT losing the configuration that says this target is normally automatic.
ALTER TABLE deployment_targets ADD COLUMN IF NOT EXISTS paused BOOLEAN NOT NULL DEFAULT FALSE;
-- What caused a deployment: merge | manual | promotion | redeploy. "Why is this running in
-- staging" must be answerable from the row itself.
ALTER TABLE deployment_runs ADD COLUMN IF NOT EXISTS trigger TEXT;

-- Per-step progress of a deployment, written AS IT HAPPENS.
--
-- The deploy used to record nothing until it finished, so a console had no data to render and
-- a crash left no trace of how far it got. Steps are appended live: [{step, status, started_at,
-- ended_at, log}], where log holds a bounded tail of that step's output — enough to see the
-- docker error, small enough that a runaway build cannot fill the table.
ALTER TABLE deployment_runs ADD COLUMN IF NOT EXISTS steps JSONB NOT NULL DEFAULT '[]'::jsonb;
-- Total wall-clock, so "how long did it take" does not require subtracting timestamps in every
-- reader that asks.
ALTER TABLE deployment_runs ADD COLUMN IF NOT EXISTS duration_ms INTEGER;

-- Health of what BOSS has DEPLOYED, sampled over time.
--
-- deployment_runs records the ACT of deploying; it says nothing about whether the thing is
-- still serving ten minutes later. A run that finished "succeeded" stays succeeded forever
-- even as the app falls over, so "is it healthy" could only ever be answered as "it deployed
-- fine", which is a different question. These samples are the missing one.
--
-- One row per probe, deliberately raw: uptime, latency percentiles and incidents are all
-- derived from this, so a new metric never needs a new table or a backfill.
CREATE TABLE IF NOT EXISTS deployment_health_samples (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  deployment_target_id TEXT NOT NULL REFERENCES deployment_targets(id),
  checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ok BOOLEAN NOT NULL,
  status_code INTEGER,
  latency_ms INTEGER,
  error TEXT,
  observed_version TEXT
);
-- The read is always "recent samples for this target", newest first.
CREATE INDEX IF NOT EXISTS idx_health_samples_target
  ON deployment_health_samples(deployment_target_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_samples_project
  ON deployment_health_samples(project_id, checked_at DESC);

-- Generalized customer-app rollout state reuses the existing deployment_rollouts
-- ledger while linking each new rollout to its ProviderOps DeploymentRun.
ALTER TABLE deployment_rollouts
  ADD COLUMN IF NOT EXISTS deployment_run_id TEXT REFERENCES deployment_runs(id);
ALTER TABLE deployment_rollouts
  ADD COLUMN IF NOT EXISTS promotion_id TEXT;
ALTER TABLE deployment_rollouts
  ADD COLUMN IF NOT EXISTS rollback_reason TEXT;
ALTER TABLE deployment_rollouts
  ADD COLUMN IF NOT EXISTS health_observations JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE deployment_rollouts
  ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE deployment_rollouts
  ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS idx_deployment_rollouts_deployment_run
  ON deployment_rollouts(deployment_run_id)
  WHERE deployment_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS deployment_promotions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  app_name TEXT NOT NULL,
  repo TEXT NOT NULL,
  commit_ref TEXT NOT NULL,
  staging_target_id TEXT NOT NULL REFERENCES deployment_targets(id),
  production_target_id TEXT NOT NULL REFERENCES deployment_targets(id),
  staging_deployment_run_id TEXT REFERENCES deployment_runs(id),
  production_deployment_run_id TEXT REFERENCES deployment_runs(id),
  production_rollout_id TEXT REFERENCES deployment_rollouts(id),
  pipeline_provider TEXT NOT NULL,
  pipeline_descriptor JSONB NOT NULL DEFAULT '{}'::jsonb,
  pipeline_verdict JSONB NOT NULL DEFAULT '{}'::jsonb,
  gate_result JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'created',
  failure_reason TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_deployment_promotions_project
  ON deployment_promotions(project_id, status, created_at DESC);

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

-- Stable resource_ref -> Terraform address mapping for the Track 1 exporter (BOSS-66).
-- The BOSS database stays canonical; these rows only pin deterministic .tf.json
-- addresses so re-exports diff cleanly and import blocks stay adoptable.
CREATE TABLE IF NOT EXISTS terraform_exports (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  changeset_id TEXT REFERENCES changesets(id),
  resource_ref TEXT NOT NULL,
  tf_address TEXT NOT NULL,
  provider TEXT NOT NULL,
  artifact_id TEXT REFERENCES artifacts(id),
  content_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, resource_ref)
);
CREATE INDEX IF NOT EXISTS idx_terraform_exports_changeset
  ON terraform_exports(project_id, changeset_id, created_at DESC);

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

-- Elastic fabric: per-pool VM/container/local worker instances (agents/projects scale these).
CREATE TABLE IF NOT EXISTS worker_instances (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  worker_pool_id TEXT NOT NULL REFERENCES worker_pools(id) ON DELETE CASCADE,
  instance_key TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'local',  -- local | vm | container
  provider TEXT,
  provider_account_id TEXT REFERENCES provider_accounts(id) ON DELETE SET NULL,
  external_id TEXT,                  -- provider resource id (droplet id, container id, …)
  status TEXT NOT NULL DEFAULT 'requested',
  -- requested|provisioning|running|draining|terminated|failed|simulated
  endpoint TEXT,
  region TEXT,
  size TEXT,
  capability_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
  scale_plan_id TEXT,  -- optional scale_plans.id (no FK: created after pools)
  changeset_id TEXT,
  resource_receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  cost_event_id TEXT REFERENCES cost_events(id) ON DELETE SET NULL,
  last_heartbeat_at TIMESTAMPTZ,
  provisioned_at TIMESTAMPTZ,
  terminated_at TIMESTAMPTZ,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, instance_key)
);
CREATE INDEX IF NOT EXISTS idx_worker_instances_pool
  ON worker_instances(project_id, worker_pool_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_worker_instances_provider
  ON worker_instances(project_id, provider, status);

-- Provider-by-provider usage ledger (API calls, compute hours, tokens, etc.).
CREATE TABLE IF NOT EXISTS provider_usage_events (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  provider_account_id TEXT REFERENCES provider_accounts(id) ON DELETE SET NULL,
  category TEXT NOT NULL,
  -- api_call | compute_hour | storage | network | model_token | infra | other
  operation TEXT NOT NULL,
  quantity DOUBLE PRECISION NOT NULL DEFAULT 1,
  unit TEXT NOT NULL DEFAULT 'count',
  cost_usd_micros BIGINT,
  resource_ref TEXT,
  worker_instance_id TEXT REFERENCES worker_instances(id) ON DELETE SET NULL,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  source TEXT NOT NULL DEFAULT 'control_plane',
  -- control_plane | provider_api | billing_sync | worker_heartbeat
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_provider_usage_project_provider
  ON provider_usage_events(project_id, provider, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_provider_usage_account
  ON provider_usage_events(project_id, provider_account_id, category, occurred_at DESC);

-- Local hybrid worker enrollments after Cloud Easy Connect (not managed fleets).
CREATE TABLE IF NOT EXISTS cloud_worker_enrollments (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  account_connect_org_id TEXT REFERENCES account_connect_orgs(id) ON DELETE SET NULL,
  worker_node_id TEXT NOT NULL,
  display_name TEXT,
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'enrolled',
  receipt_id TEXT,
  enrolled_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, worker_node_id)
);
CREATE INDEX IF NOT EXISTS idx_cloud_worker_enrollments_project
  ON cloud_worker_enrollments(project_id, status, created_at DESC);

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
-- Terraform drift-sync (BOSS-69): per-resource three-way comparison payload
-- (BOSS desired_state vs supplied .tf vs live inventory, with affected
-- resource_refs). A customer .tf never mutates desired state — divergence only
-- ever yields this report plus a proposed corrective ChangeSet.
ALTER TABLE drift_reports
  ADD COLUMN IF NOT EXISTS detail JSONB NOT NULL DEFAULT '{}'::jsonb;

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
  operational_intensity INTEGER,  -- CEO budget slider 0-100 (null = caps set directly)
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Additive migration: operational_intensity post-dates the original budget_policies table.
-- CREATE TABLE IF NOT EXISTS won't add columns to an existing table, so deployments created
-- before this column need it backfilled here (matches the CREATE: nullable INTEGER, no default).
ALTER TABLE budget_policies ADD COLUMN IF NOT EXISTS operational_intensity INTEGER;

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

-- Job-scoped model capacity.  Agents receive a short-lived opaque lease, never
-- provider credential material.  The worker's durable job lease remains the
-- root authority and every paid lease is tied to a BudgetReservation.
CREATE TABLE IF NOT EXISTS model_gateway_leases (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  route_decision_id TEXT NOT NULL REFERENCES routing_logs(id),
  entitlement_source_id TEXT NOT NULL REFERENCES entitlement_sources(id),
  entitlement_pool_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT,
  auth_method TEXT NOT NULL,
  credential_secret_ref_id TEXT REFERENCES secret_refs(id) ON DELETE SET NULL,
  consumer_owner_id TEXT,
  lease_token_hash TEXT NOT NULL,
  fencing_token BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'completed', 'failed', 'released', 'expired')),
  estimated_cost_usd_micros BIGINT NOT NULL DEFAULT 0 CHECK (estimated_cost_usd_micros >= 0),
  budget_reservation_id TEXT REFERENCES budget_reservations(id),
  allocation_id TEXT REFERENCES resource_allocations(id),
  max_usage JSONB NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key TEXT NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(project_id, dedupe_key),
  UNIQUE(entitlement_source_id, fencing_token)
);
CREATE INDEX IF NOT EXISTS idx_model_gateway_leases_active_source
  ON model_gateway_leases(entitlement_source_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_model_gateway_leases_job
  ON model_gateway_leases(project_id, job_id, acquired_at DESC);

-- A lease can start an auditable provider effect exactly once.  Request bodies
-- are deliberately absent: only a caller-supplied hash and non-secret metadata
-- are retained for replay detection and observability.
CREATE TABLE IF NOT EXISTS model_gateway_invocations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  model_gateway_lease_id TEXT NOT NULL REFERENCES model_gateway_leases(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL REFERENCES receipts(id),
  request_hash TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'in_progress'
    CHECK (status IN ('in_progress', 'succeeded', 'failed')),
  provider_ref TEXT,
  usage_units JSONB NOT NULL DEFAULT '{}'::jsonb,
  actual_cost_usd_micros BIGINT CHECK (actual_cost_usd_micros >= 0),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_model_gateway_invocations_lease
  ON model_gateway_invocations(model_gateway_lease_id, started_at DESC);

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

-- Operator remediations attached to a deadletter (A5 / BOSS-342 residual).
-- Every retry/requeue/hold/escalate writes a row so operator actions never silently drop.
CREATE TABLE IF NOT EXISTS deadletter_remediations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  deadletter_id TEXT NOT NULL REFERENCES deadletters(id),
  action TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  actor TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  requeued_job_id TEXT,
  decision_id TEXT,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_deadletter_remediations_project
  ON deadletter_remediations(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deadletter_remediations_dlq
  ON deadletter_remediations(deadletter_id, created_at DESC);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_github_reconciliation_run_deadletter'
  ) THEN
    ALTER TABLE github_reconciliation_runs
      ADD CONSTRAINT fk_github_reconciliation_run_deadletter
      FOREIGN KEY (deadletter_id)
      REFERENCES deadletters(id) ON DELETE SET NULL;
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  data JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE events
  ALTER COLUMN created_at SET DEFAULT clock_timestamp();
CREATE INDEX IF NOT EXISTS idx_events_project_created ON events(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS event_subscriptions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  sink_kind TEXT NOT NULL,
  sink_ref TEXT NOT NULL,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  enabled BOOLEAN NOT NULL DEFAULT true,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, event_type, sink_kind, sink_ref)
);
CREATE INDEX IF NOT EXISTS idx_event_subscriptions_match
  ON event_subscriptions(project_id, enabled, event_type);

CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  secret_ref TEXT NOT NULL,
  description TEXT,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  enabled BOOLEAN NOT NULL DEFAULT true,
  deleted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_project
  ON webhook_endpoints(project_id, enabled, created_at DESC)
  WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  endpoint_id TEXT NOT NULL REFERENCES webhook_endpoints(id),
  event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  dedupe_key TEXT NOT NULL,
  request_body JSONB NOT NULL DEFAULT '{}'::jsonb,
  response JSONB,
  last_error JSONB,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  next_attempt_at TIMESTAMPTZ,
  delivered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_project_status
  ON webhook_deliveries(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS inbound_webhooks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  delivery_key TEXT NOT NULL,
  repo_full_name TEXT,
  provider_event TEXT NOT NULL,
  action TEXT,
  status TEXT NOT NULL,
  signature_status TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(source, delivery_key)
);
CREATE INDEX IF NOT EXISTS idx_inbound_webhooks_project_created
  ON inbound_webhooks(project_id, created_at DESC);
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_github_reconciliation_signal_inbound'
  ) THEN
    ALTER TABLE github_reconciliation_signals
      ADD CONSTRAINT fk_github_reconciliation_signal_inbound
      FOREIGN KEY (inbound_webhook_id)
      REFERENCES inbound_webhooks(id) ON DELETE SET NULL;
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS feature_catalog (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  label TEXT,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  doc_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Planned',
  source TEXT,
  implementation_files JSONB NOT NULL DEFAULT '[]'::jsonb,
  test_coverage JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  needs_label BOOLEAN NOT NULL DEFAULT false,
  last_documented_event_id TEXT,
  last_documented_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, label),
  UNIQUE(project_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_feature_catalog_project_status
  ON feature_catalog(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_threads (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  actor TEXT NOT NULL,
  surface TEXT NOT NULL DEFAULT 'chat',
  external_ref TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  pending_plan_id TEXT REFERENCES plans(id) ON DELETE SET NULL,
  pending_plan_summary JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (surface IN ('chat', 'github')),
  CHECK (status IN ('active', 'closed'))
);
CREATE INDEX IF NOT EXISTS idx_agent_threads_project_actor
  ON agent_threads(project_id, actor, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_threads_surface_external
  ON agent_threads(project_id, surface, external_ref)
  WHERE external_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_messages (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
  actions JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (role IN ('user', 'assistant', 'tool'))
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_thread_created
  ON agent_messages(thread_id, created_at, id);

-- Metatron Captain: goals, durable memory, watch ticks (always-on oversight).
-- Chat persistence reuses agent_threads (surface=chat, external_ref=metatron:captain).
CREATE TABLE IF NOT EXISTS metatron_goals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  target JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_score NUMERIC,
  last_measured_at TIMESTAMPTZ,
  last_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  auto_drive BOOLEAN NOT NULL DEFAULT false,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (kind IN ('working_software', 'budget', 'queue', 'quality', 'custom')),
  CHECK (status IN ('active', 'paused', 'achieved', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_metatron_goals_project
  ON metatron_goals(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS metatron_memories (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  thread_id TEXT,
  kind TEXT NOT NULL DEFAULT 'fact',
  content TEXT NOT NULL,
  salience NUMERIC NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL DEFAULT 'chat',
  refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (kind IN ('fact', 'decision', 'lesson', 'goal_note', 'watch')),
  CHECK (salience >= 0 AND salience <= 1)
);
CREATE INDEX IF NOT EXISTS idx_metatron_memories_project
  ON metatron_memories(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS metatron_watch_ticks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  observed JSONB NOT NULL DEFAULT '{}'::jsonb,
  findings JSONB NOT NULL DEFAULT '[]'::jsonb,
  proposed_moves JSONB NOT NULL DEFAULT '[]'::jsonb,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cycle TEXT NOT NULL DEFAULT 'watch',
  verify JSONB NOT NULL DEFAULT '{}'::jsonb,
  applied JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_metatron_watch_project
  ON metatron_watch_ticks(project_id, created_at DESC);

-- Focused always-on agents (thin builder + duty loop; Studio stays the full registry).
CREATE TABLE IF NOT EXISTS focused_agents (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_key TEXT NOT NULL,
  name TEXT NOT NULL,
  focus TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  mode TEXT NOT NULL DEFAULT 'hybrid',
  interval_seconds INTEGER NOT NULL DEFAULT 120,
  wake_on JSONB NOT NULL DEFAULT '[]'::jsonb,
  allowed_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
  last_tick_at TIMESTAMPTZ,
  last_wake_at TIMESTAMPTZ,
  last_status TEXT,
  last_score NUMERIC,
  last_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, agent_key)
);
CREATE INDEX IF NOT EXISTS idx_focused_agents_project
  ON focused_agents(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS focused_agent_ticks (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES focused_agents(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL,
  observed JSONB NOT NULL DEFAULT '{}'::jsonb,
  planned JSONB NOT NULL DEFAULT '[]'::jsonb,
  validation JSONB NOT NULL DEFAULT '{}'::jsonb,
  applied JSONB NOT NULL DEFAULT '[]'::jsonb,
  rejected JSONB NOT NULL DEFAULT '[]'::jsonb,
  verify JSONB NOT NULL DEFAULT '{}'::jsonb,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_focused_agent_ticks_agent
  ON focused_agent_ticks(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mcp_servers (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  transport TEXT NOT NULL DEFAULT 'http',
  risk_tier INTEGER NOT NULL DEFAULT 1,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  enabled BOOLEAN NOT NULL DEFAULT true,
  description TEXT,
  publisher JSONB NOT NULL DEFAULT '{}'::jsonb,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  tool_risk_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_project ON mcp_servers(project_id, enabled);

CREATE TABLE IF NOT EXISTS project_skills (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  skill_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'tenant',
  enabled BOOLEAN NOT NULL DEFAULT true,
  on_demand BOOLEAN NOT NULL DEFAULT false,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  publisher JSONB NOT NULL DEFAULT '{}'::jsonb,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, skill_id),
  CHECK (source IN ('system', 'tenant', 'module'))
);
CREATE INDEX IF NOT EXISTS idx_project_skills_project
  ON project_skills(project_id, enabled, on_demand);

-- Model-agnostic agent toolchain intake. Third-party prompts, hooks, MCP
-- configuration, commands, and scripts are untrusted supply-chain inputs.
-- BOSS records immutable provenance and scan evidence but never stores or
-- executes raw imported source from this registry.
CREATE TABLE IF NOT EXISTS toolchain_sources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  repository_url TEXT NOT NULL,
  pinned_revision TEXT NOT NULL,
  license_id TEXT NOT NULL,
  license_status TEXT NOT NULL
    CHECK (license_status IN ('allowed', 'mixed', 'missing', 'restricted')),
  integration_mode TEXT NOT NULL
    CHECK (integration_mode IN (
      'pattern_only', 'selective_import', 'catalog_only', 'blocked'
    )),
  status TEXT NOT NULL DEFAULT 'declared'
    CHECK (status IN ('declared', 'active', 'quarantined', 'blocked')),
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  adopted_components JSONB NOT NULL DEFAULT '[]'::jsonb,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'bootstrap',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, source_key)
);
CREATE INDEX IF NOT EXISTS idx_toolchain_sources_project
  ON toolchain_sources(project_id, status, source_key);

CREATE TABLE IF NOT EXISTS toolchain_import_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES toolchain_sources(id) ON DELETE CASCADE,
  revision TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('approved', 'review_required', 'quarantined', 'rejected')),
  file_count INTEGER NOT NULL CHECK (file_count >= 0),
  total_bytes BIGINT NOT NULL CHECK (total_bytes >= 0),
  inventory JSONB NOT NULL DEFAULT '{}'::jsonb,
  findings JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by TEXT,
  reviewed_at TIMESTAMPTZ,
  review_note TEXT,
  UNIQUE(project_id, source_id, snapshot_hash)
);
CREATE INDEX IF NOT EXISTS idx_toolchain_import_runs_project
  ON toolchain_import_runs(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS toolchain_candidates (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  import_run_id TEXT NOT NULL REFERENCES toolchain_import_runs(id) ON DELETE CASCADE,
  source_path TEXT NOT NULL,
  candidate_kind TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN (
      'metadata_only', 'review_required', 'quarantined', 'approved', 'rejected'
    )),
  risk_tier INTEGER NOT NULL DEFAULT 0 CHECK (risk_tier BETWEEN 0 AND 3),
  findings JSONB NOT NULL DEFAULT '[]'::jsonb,
  normalized_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by TEXT,
  reviewed_at TIMESTAMPTZ,
  review_note TEXT,
  UNIQUE(import_run_id, source_path)
);
CREATE INDEX IF NOT EXISTS idx_toolchain_candidates_project
  ON toolchain_candidates(project_id, status, risk_tier, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_id TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  messages JSONB NOT NULL DEFAULT '[]'::jsonb,
  transcript JSONB NOT NULL DEFAULT '[]'::jsonb,
  usage JSONB NOT NULL DEFAULT '{}'::jsonb,
  step INTEGER NOT NULL DEFAULT 0,
  pending JSONB,
  finalize_ctx JSONB,
  output_text TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_project ON agent_runs(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS meeting_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_id TEXT,
  status TEXT NOT NULL DEFAULT 'running',     -- running | completed | failed
  phase TEXT NOT NULL DEFAULT 'research',      -- research|round1|round2|synthesis|draft|finalize|done
  directive TEXT NOT NULL,
  url TEXT,
  role_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
  constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
  state JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {research, round1, round2, synthesis, prd_markdown}
  usage JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {cost_usd_micros, model}
  finalize_ctx JSONB NOT NULL DEFAULT '{}'::jsonb,
  meeting_id TEXT,
  minutes_artifact_id TEXT,
  decision_id TEXT,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_meeting_runs_project ON meeting_runs(project_id, status, updated_at DESC);

-- Durable coding-agent task: clone -> edit -> build/test -> open PR (gated).
-- Unlike meeting_runs (pure-data phases), a code task's working tree is
-- filesystem-ephemeral, so the whole pipeline runs in ONE job execution;
-- crash-resumability comes from job lease-reclaim + the PR receipt dedupe
-- (no double-open). The row tracks status/phase for observability + idempotency.
CREATE TABLE IF NOT EXISTS code_task_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_id TEXT,
  status TEXT NOT NULL DEFAULT 'running',     -- running | completed | failed | blocked
  phase TEXT NOT NULL DEFAULT 'queued',       -- queued|provisioning|coding|validating|opening_pr|done
  title TEXT NOT NULL,
  spec JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {instructions, acceptance_criteria, test_commands, owner, repo, base_branch}
  state JSONB NOT NULL DEFAULT '{}'::jsonb,    -- {summary, diff_summary, tests, tests_passed, turns}
  usage JSONB NOT NULL DEFAULT '{}'::jsonb,    -- {cost_usd_micros, model}
  finalize_ctx JSONB NOT NULL DEFAULT '{}'::jsonb,
  reservation_id TEXT,
  pull_request_url TEXT,
  pull_request_number INTEGER,
  decision_id TEXT,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_code_task_runs_project ON code_task_runs(project_id, status, updated_at DESC);

-- Durable, versioned measurements derived from terminal QA benchmark runs.  These are
-- analysis snapshots only: a low score may trigger a visible recommendation, but cannot
-- alter a routing/workflow baseline or create an applied change by itself.
CREATE TABLE IF NOT EXISTS qa_benchmark_evaluations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  code_task_run_id TEXT NOT NULL REFERENCES code_task_runs(id) ON DELETE CASCADE,
  benchmark_id TEXT NOT NULL,
  scorecard_version TEXT NOT NULL,
  status TEXT NOT NULL, -- ready_for_candidate | not_ready
  overall_score DOUBLE PRECISION,
  observed_weight INTEGER NOT NULL DEFAULT 0,
  report JSONB NOT NULL DEFAULT '{}'::jsonb,
  recommendation JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_hash TEXT NOT NULL,
  evaluated_by TEXT NOT NULL DEFAULT 'qa_benchmark_scheduler',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(code_task_run_id, scorecard_version)
);
CREATE INDEX IF NOT EXISTS idx_qa_benchmark_evaluations_project_created
  ON qa_benchmark_evaluations(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qa_benchmark_evaluations_benchmark_status
  ON qa_benchmark_evaluations(project_id, benchmark_id, status, updated_at DESC);

-- Governed autonomous-improvement programs pin a known-good measurement contract.
-- Programs and trials are evidence/control records only: a winning trial may create a
-- normal self-improvement proposal, but it cannot apply its candidate or move a baseline.
CREATE TABLE IF NOT EXISTS improvement_programs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  objective TEXT NOT NULL,
  benchmark_id TEXT NOT NULL,
  baseline_evaluation_id TEXT NOT NULL REFERENCES qa_benchmark_evaluations(id),
  metric_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
  metric_contract_hash TEXT NOT NULL,
  minimum_evidence_weight INTEGER NOT NULL DEFAULT 50 CHECK (minimum_evidence_weight BETWEEN 1 AND 100),
  minimum_overall_delta DOUBLE PRECISION NOT NULL DEFAULT 1,
  maximum_dimension_regression DOUBLE PRECISION NOT NULL DEFAULT 0,
  hard_gates JSONB NOT NULL DEFAULT '{"functionality": 100}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active', -- active | paused | retired
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_improvement_programs_project_status
  ON improvement_programs(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS improvement_trials (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  program_id TEXT NOT NULL REFERENCES improvement_programs(id) ON DELETE CASCADE,
  control_evaluation_id TEXT NOT NULL REFERENCES qa_benchmark_evaluations(id),
  candidate_evaluation_id TEXT NOT NULL REFERENCES qa_benchmark_evaluations(id),
  candidate_change_ref TEXT NOT NULL,
  candidate_diff_summary TEXT NOT NULL,
  changed_variable TEXT NOT NULL,
  control_configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
  candidate_configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
  isolation_evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL, -- candidate_wins | rejected | inconclusive | proposed | adopted | superseded
  verdict JSONB NOT NULL DEFAULT '{}'::jsonb,
  verdict_hash TEXT NOT NULL,
  proposal_id TEXT REFERENCES self_improvement_proposals(id),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (control_evaluation_id <> candidate_evaluation_id),
  UNIQUE(program_id, candidate_evaluation_id, candidate_change_ref)
);
CREATE INDEX IF NOT EXISTS idx_improvement_trials_program_status
  ON improvement_trials(program_id, status, updated_at DESC);

-- Fail-closed quality evidence for a governed coding-pipeline run (see QualityGate ObjectSpec).
CREATE TABLE IF NOT EXISTS quality_gates (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_id TEXT,
  code_task_run_id TEXT,
  risk_tier INTEGER NOT NULL DEFAULT 2,
  test_author_independent BOOLEAN NOT NULL DEFAULT false,
  reviewer_independent BOOLEAN NOT NULL DEFAULT false,
  tests_passed BOOLEAN NOT NULL DEFAULT false,
  adversarial_verdict TEXT NOT NULL DEFAULT 'not_run',  -- survived | refuted | not_run
  status TEXT NOT NULL DEFAULT 'fail',                  -- pass | fail | waived
  fail_closed BOOLEAN NOT NULL DEFAULT true,
  gates_passed JSONB NOT NULL DEFAULT '[]'::jsonb,
  gates_failed JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by_job_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_quality_gates_run ON quality_gates(code_task_run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_quality_gates_run ON quality_gates(project_id, code_task_run_id);
-- Phase 3 (factory scale-up): explicit lint + CI-green gates, second-model cross-check, a numeric
-- merge-confidence score, and a stored review packet artifact.
ALTER TABLE quality_gates ADD COLUMN IF NOT EXISTS lint_passed BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE quality_gates ADD COLUMN IF NOT EXISTS ci_green BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE quality_gates ADD COLUMN IF NOT EXISTS cross_check_agreement BOOLEAN;
ALTER TABLE quality_gates ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE quality_gates ADD COLUMN IF NOT EXISTS review_packet_artifact_id TEXT;

-- Custom pipeline cost/rigor profiles per project (presets live in code; these are overrides
-- and customer-defined profiles). is_default marks the project's chosen profile.
CREATE TABLE IF NOT EXISTS pipeline_profiles (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  label TEXT,
  tier_floor INTEGER NOT NULL DEFAULT 0,
  tier_cap INTEGER NOT NULL DEFAULT 3,
  model_routing JSONB NOT NULL DEFAULT '{}'::jsonb,
  caching BOOLEAN NOT NULL DEFAULT true,
  parallel_checks BOOLEAN NOT NULL DEFAULT true,
  is_default BOOLEAN NOT NULL DEFAULT false,
  created_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_profiles_name ON pipeline_profiles(project_id, name);

CREATE TABLE IF NOT EXISTS meeting_roles (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  role_key TEXT NOT NULL,
  title TEXT NOT NULL,
  lens TEXT NOT NULL,
  risk_tier INTEGER NOT NULL DEFAULT 1,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  is_default BOOLEAN NOT NULL DEFAULT false,
  enabled BOOLEAN NOT NULL DEFAULT true,
  sort_order INTEGER NOT NULL DEFAULT 100,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, role_key)
);

CREATE TABLE IF NOT EXISTS canary_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL,            -- pass | fail
  results JSONB NOT NULL,          -- per-canary {canary, status, detail}
  passed INTEGER NOT NULL,
  failed INTEGER NOT NULL,
  skipped INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_canary_runs_project ON canary_runs (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS policy_eval_log (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  boundary TEXT NOT NULL,        -- budget.reserve | comms.delivery | data_boundary | approval | ...
  action TEXT,                   -- what was being attempted
  decision TEXT NOT NULL,        -- allow | deny | defer
  reason TEXT,
  detail JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_policy_eval_log_project ON policy_eval_log (project_id, created_at DESC);

-- Phase 2: eval-harness regression suites (routing/policy/factory/autonomy quality). Run on demand
-- or nightly; results gate self-improvement rollouts. Mirrors canary_runs but carries a suite label.
CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  suite TEXT NOT NULL,             -- regression suite name (e.g. 'all', 'routing', 'policy')
  status TEXT NOT NULL,            -- pass | fail
  results JSONB NOT NULL,          -- per-eval {eval, status, detail, metrics}
  passed INTEGER NOT NULL,
  failed INTEGER NOT NULL,
  skipped INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_project ON eval_runs (project_id, created_at DESC);

-- Phase 2: staged rollout state for the self-improvement runtime (stages, current index, history,
-- auto-rollback thresholds). Empty {} until a staged rollout begins.
ALTER TABLE self_improvement_proposals
  ADD COLUMN IF NOT EXISTS rollout_state JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Phase 2: skill-invocation framework records. Every invocation through the runtime (mock or real)
-- records the permission decision, cost estimate, retry attempts, and outcome. Mock mode runs the
-- full governance gauntlet with no real side effects (used by canaries).
CREATE TABLE IF NOT EXISTS skill_invocations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  created_by_job_id TEXT REFERENCES jobs(id),
  skill_id TEXT NOT NULL,
  caller TEXT NOT NULL,
  mode TEXT NOT NULL,                       -- mock | real
  status TEXT NOT NULL,                     -- succeeded | denied | failed
  permission_decision TEXT NOT NULL,        -- allow | deny
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 1,
  cost_estimate_usd_micros BIGINT NOT NULL DEFAULT 0,
  actual_cost_usd_micros BIGINT,
  cost_receipt_id TEXT REFERENCES cost_receipts(id),
  reservation_id TEXT,
  reason TEXT,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE skill_invocations
  ADD COLUMN IF NOT EXISTS actual_cost_usd_micros BIGINT;
ALTER TABLE skill_invocations
  ADD COLUMN IF NOT EXISTS cost_receipt_id TEXT REFERENCES cost_receipts(id);
ALTER TABLE skill_invocations
  ADD COLUMN IF NOT EXISTS created_by_job_id TEXT REFERENCES jobs(id);
CREATE INDEX IF NOT EXISTS idx_skill_invocations_project
  ON skill_invocations (project_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_invocations_real_job
  ON skill_invocations(created_by_job_id)
  WHERE created_by_job_id IS NOT NULL AND mode = 'real';

-- Phase 3 (comms robustness): inbound acknowledgment ledger — one row per inbox message, advanced
-- through received → parsed → routed → (actioned | deadlettered | acknowledged). The durable proof
-- of "no silent drops": every inbound reaches a terminal ack state, with the full transition history.
CREATE TABLE IF NOT EXISTS ack_state (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  inbox_message_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  state TEXT NOT NULL,             -- received | parsed | routed | actioned | deadlettered | acknowledged
  terminal BOOLEAN NOT NULL DEFAULT false,
  route_kind TEXT,                 -- decision | informational | ...
  target_ref TEXT,                 -- e.g. decision_id the command routed to
  reason TEXT,
  remediation TEXT,
  history JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(inbox_message_id)
);
CREATE INDEX IF NOT EXISTS idx_ack_state_project
  ON ack_state (project_id, state, updated_at DESC);

-- Phase 4 (testing depth): chaos/load fault-injection runs. Each row records a controlled fault
-- injected against the live primitives and whether the system recovered/detected it.
CREATE TABLE IF NOT EXISTS chaos_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  scenario TEXT NOT NULL,          -- lease_reclaim | worker_crash | deadletter_storm
  injected BOOLEAN NOT NULL DEFAULT true,
  recovered BOOLEAN NOT NULL DEFAULT false,
  detail TEXT,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chaos_runs_project ON chaos_runs (project_id, created_at DESC);

-- Phase 4 (finops): InfraCommitment reconciler output — committed spend vs actual usage per
-- commitment period, with a verdict (on_track | under_utilized | over_utilized) and any review decision.
CREATE TABLE IF NOT EXISTS commitment_reconciliations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  commitment_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  committed_usd_micros BIGINT NOT NULL,
  actual_usd_micros BIGINT NOT NULL,
  utilization DOUBLE PRECISION NOT NULL,
  verdict TEXT NOT NULL,
  decision_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_commitment_reconciliations_project
  ON commitment_reconciliations (project_id, created_at DESC);

-- Tier-1 "1-box" app previews: ephemeral local runs of a code-task's built app, so an
-- operator can see the working website before merge. Process-based, auto-expiring (TTL).
CREATE TABLE IF NOT EXISTS app_previews (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  run_id TEXT,
  repo TEXT,
  branch TEXT,
  command TEXT,
  port INTEGER,
  url TEXT,
  pid INTEGER,
  mode TEXT NOT NULL DEFAULT 'process',       -- process | docker | rootfs
  container_id TEXT,                           -- docker container id (mode=docker)
  substrate TEXT NOT NULL DEFAULT 'preview_host', -- preview_host|preview_docker|preview_rootfs
  build_substrate TEXT,                        -- local_subprocess|microvm
  sandbox_task_id TEXT,                        -- retained rootfs backing a rootfs preview
  status TEXT NOT NULL DEFAULT 'starting',   -- starting|running|failed|stopped|expired
  failure_kind TEXT,                           -- clone_failed|command_missing|boot_crash|port_timeout
  error TEXT,
  log_path TEXT,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE app_previews ADD COLUMN IF NOT EXISTS substrate TEXT NOT NULL DEFAULT 'preview_host';
ALTER TABLE app_previews ADD COLUMN IF NOT EXISTS build_substrate TEXT;
ALTER TABLE app_previews ADD COLUMN IF NOT EXISTS sandbox_task_id TEXT;
ALTER TABLE app_previews ADD COLUMN IF NOT EXISTS failure_kind TEXT;
CREATE INDEX IF NOT EXISTS idx_app_previews_project ON app_previews (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_previews_run ON app_previews (run_id);

-- Durable, per-lineage feedback conversation.  Dispatch is confirmation-gated;
-- messages preserve the operator request, clarification, proposal and retry link.
CREATE TABLE IF NOT EXISTS feedback_threads (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  root_run_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'open',
  pending_proposal JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS feedback_messages (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES feedback_threads(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  resulting_run_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_feedback_messages_thread
  ON feedback_messages (thread_id, created_at);

-- Discovery pipeline: research-driven outcome discovery (signal sweep -> synthesize -> vet ->
-- shape -> evidence gate). state holds the cited Discovery Dossier + candidate outcomes.
CREATE TABLE IF NOT EXISTS discovery_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running | completed | failed
    phase TEXT NOT NULL DEFAULT 'queued',     -- queued|signal_sweep|synthesize|vet|shape|gate|done
    title TEXT NOT NULL,
    seed TEXT,                                 -- the raw idea / problem / signal
    context TEXT,                              -- operator context (ICP, market, constraints)
    state JSONB NOT NULL DEFAULT '{}',         -- the Discovery Dossier (brief/findings/analysis/critique/outcomes/gate)
    usage JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_discovery_runs_project ON discovery_runs (project_id, created_at DESC);

-- VAG Phase 0 (docs/product/BOSS_VERIFICATION_AMPLIFIED_GENERATION.md): one row per LLM
-- invocation, so $/verified-outcome, pass-rate, and escalation rate can be measured per
-- (model, stage, task type). Written best-effort by services/llm_telemetry.py — a failed
-- insert never fails the pipeline.
CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    code_task_run_id TEXT,
    stage TEXT,                                -- standards|spec|plan|review_plan|write_tests|implement|review_code|security_check|consistency_check|check_tests|solo|simple|guest|research|...
    engine TEXT,                               -- claude_code | native | guest engine name
    model TEXT,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd_micros BIGINT NOT NULL DEFAULT 0,
    price_estimated BOOLEAN NOT NULL DEFAULT false,  -- true = model missing from the price map (DEFAULT_PRICE used)
    latency_ms INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    ok BOOLEAN NOT NULL DEFAULT true,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls (code_task_run_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls (model, stage, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_project ON llm_calls (project_id, created_at DESC);

-- Reusable mapped workflow runtime (WFL-001 / Plane BOSS-134..143).  Definitions
-- are stable identities; published versions are immutable content-addressed
-- graphs.  Mappings deterministically select a version for a class of work and
-- runs/steps retain the exact version, policy snapshot, leases, and evidence.
CREATE TABLE IF NOT EXISTS workflow_definitions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  workflow_key TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('draft', 'active', 'retired')),
  active_version_id TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, workflow_key)
);
CREATE INDEX IF NOT EXISTS idx_workflow_definitions_project
  ON workflow_definitions(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS workflow_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  workflow_definition_id TEXT NOT NULL REFERENCES workflow_definitions(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'published'
    CHECK (status IN ('published', 'retired')),
  spec JSONB NOT NULL,
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workflow_definition_id, version),
  UNIQUE(workflow_definition_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_workflow_versions_project
  ON workflow_versions(project_id, workflow_definition_id, version DESC);

CREATE TABLE IF NOT EXISTS workflow_mappings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  workflow_version_id TEXT NOT NULL REFERENCES workflow_versions(id),
  selector_kind TEXT NOT NULL,
  selector_value TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('org', 'workspace', 'project')),
  scope_id TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_workflow_mappings_resolve
  ON workflow_mappings(project_id, selector_kind, selector_value, enabled, priority DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_mappings_active_selector
  ON workflow_mappings(project_id, selector_kind, selector_value, scope_type, scope_id, priority)
  WHERE enabled = true;

CREATE TABLE IF NOT EXISTS workflow_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  workflow_definition_id TEXT NOT NULL REFERENCES workflow_definitions(id),
  workflow_version_id TEXT NOT NULL REFERENCES workflow_versions(id),
  workflow_mapping_id TEXT REFERENCES workflow_mappings(id),
  parent_run_id TEXT REFERENCES workflow_runs(id),
  root_run_id TEXT,
  actor TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN (
      'queued', 'validating', 'running', 'waiting', 'paused', 'blocked',
      'completed', 'failed', 'deadlettered', 'cancelled', 'compensating', 'compensated'
    )),
  input JSONB NOT NULL DEFAULT '{}'::jsonb,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  current_frontier JSONB NOT NULL DEFAULT '[]'::jsonb,
  dedupe_key TEXT,
  blocked_reason TEXT,
  error TEXT,
  paused_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_runs_dedupe
  ON workflow_runs(project_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_runs_project_state
  ON workflow_runs(project_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_parent
  ON workflow_runs(parent_run_id, created_at);

CREATE TABLE IF NOT EXISTS workflow_step_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
  step_id TEXT NOT NULL,
  step_kind TEXT NOT NULL,
  iteration INTEGER NOT NULL DEFAULT 0 CHECK (iteration >= 0),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN (
      'pending', 'ready', 'leased', 'running', 'waiting', 'succeeded',
      'skipped', 'failed', 'deadlettered', 'cancelled'
    )),
  input JSONB NOT NULL DEFAULT '{}'::jsonb,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  binding_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  decision_id TEXT REFERENCES decisions(id),
  child_workflow_run_id TEXT REFERENCES workflow_runs(id),
  last_error TEXT,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workflow_run_id, step_id, iteration)
);
CREATE INDEX IF NOT EXISTS idx_workflow_steps_claim
  ON workflow_step_runs(project_id, status, run_after, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_workflow_steps_run
  ON workflow_step_runs(workflow_run_id, iteration, created_at);

CREATE TABLE IF NOT EXISTS workflow_migrations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
  from_version_id TEXT NOT NULL REFERENCES workflow_versions(id),
  to_version_id TEXT NOT NULL REFERENCES workflow_versions(id),
  status TEXT NOT NULL CHECK (status IN ('completed', 'rolled_back', 'failed')),
  compatibility JSONB NOT NULL DEFAULT '{}'::jsonb,
  rollback_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  reason TEXT NOT NULL,
  created_by TEXT NOT NULL,
  rolled_back_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  rolled_back_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_workflow_migrations_run
  ON workflow_migrations(workflow_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS workflow_compensation_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
  workflow_step_run_id TEXT NOT NULL REFERENCES workflow_step_runs(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK (sequence >= 0),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'ready', 'leased', 'running', 'succeeded', 'failed', 'deadlettered', 'cancelled')),
  action JSONB NOT NULL,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_workflow_compensation_claim
  ON workflow_compensation_runs(project_id, workflow_run_id, status, sequence);

-- Agent Studio, executable panels, and the global agent-to-agent coordination
-- fabric (BOSS-256..266). Stable definitions point at immutable versions.
-- Runtime authority is still resolved from RoleSpecs, Agent Modules, Skills,
-- Integrations, policies, budgets, leases and data boundaries at execution time.
CREATE TABLE IF NOT EXISTS agent_definitions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_key TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  owner TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'active', 'retired')),
  active_version_id TEXT,
  source TEXT NOT NULL DEFAULT 'operator',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, agent_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_definitions_project
  ON agent_definitions(project_id, status, updated_at DESC);
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'delivery_bot_identities_agent_definition_id_fkey'
  ) THEN
    ALTER TABLE delivery_bot_identities
      ADD CONSTRAINT delivery_bot_identities_agent_definition_id_fkey
      FOREIGN KEY (agent_definition_id) REFERENCES agent_definitions(id);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS agent_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_definition_id TEXT NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'published', 'retired')),
  role_key TEXT NOT NULL,
  agent_module_id TEXT,
  model_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  prompts JSONB NOT NULL DEFAULT '{}'::jsonb,
  rules JSONB NOT NULL DEFAULT '[]'::jsonb,
  requested_skills JSONB NOT NULL DEFAULT '[]'::jsonb,
  requested_integrations JSONB NOT NULL DEFAULT '[]'::jsonb,
  scopes JSONB NOT NULL DEFAULT '{}'::jsonb,
  limits JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
  a2a_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  validation JSONB NOT NULL DEFAULT '{}'::jsonb,
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  retired_at TIMESTAMPTZ,
  UNIQUE(agent_definition_id, version),
  UNIQUE(agent_definition_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_agent_versions_project
  ON agent_versions(project_id, agent_definition_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_agent_versions_role
  ON agent_versions(project_id, role_key, status);

CREATE TABLE IF NOT EXISTS agent_test_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_version_id TEXT NOT NULL REFERENCES agent_versions(id) ON DELETE CASCADE,
  scenario JSONB NOT NULL DEFAULT '{}'::jsonb,
  compiled_context JSONB NOT NULL DEFAULT '{}'::jsonb,
  assertions JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'passed', 'failed', 'blocked', 'cancelled')),
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_agent_test_runs_version
  ON agent_test_runs(project_id, agent_version_id, created_at DESC);

CREATE TABLE IF NOT EXISTS panel_definitions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  panel_key TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  purpose TEXT NOT NULL,
  owner TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'active', 'retired')),
  active_version_id TEXT,
  source TEXT NOT NULL DEFAULT 'operator',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, panel_key)
);
CREATE INDEX IF NOT EXISTS idx_panel_definitions_project
  ON panel_definitions(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS panel_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  panel_definition_id TEXT NOT NULL REFERENCES panel_definitions(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'published', 'retired')),
  topology TEXT NOT NULL,
  context_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  independence_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  quorum_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  bounds JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  consolidation_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  dissent_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  escalation_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
  validation JSONB NOT NULL DEFAULT '{}'::jsonb,
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  retired_at TIMESTAMPTZ,
  UNIQUE(panel_definition_id, version),
  UNIQUE(panel_definition_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_panel_versions_project
  ON panel_versions(project_id, panel_definition_id, version DESC);

CREATE TABLE IF NOT EXISTS panel_version_members (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  panel_version_id TEXT NOT NULL REFERENCES panel_versions(id) ON DELETE CASCADE,
  member_key TEXT NOT NULL,
  agent_version_id TEXT NOT NULL REFERENCES agent_versions(id),
  weight DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (weight > 0),
  required BOOLEAN NOT NULL DEFAULT true,
  perspective TEXT,
  private_context JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(panel_version_id, member_key)
);
CREATE INDEX IF NOT EXISTS idx_panel_version_members_agent
  ON panel_version_members(project_id, agent_version_id);

CREATE TABLE IF NOT EXISTS panel_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  panel_version_id TEXT NOT NULL REFERENCES panel_versions(id),
  task_id TEXT REFERENCES tasks(id),
  workflow_run_id TEXT REFERENCES workflow_runs(id),
  agent_group_run_id TEXT,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN (
      'queued', 'resolving', 'running', 'waiting_member', 'waiting_decision',
      'blocked', 'completed', 'failed', 'deadlettered', 'cancelled'
    )),
  input JSONB NOT NULL DEFAULT '{}'::jsonb,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  consolidation JSONB NOT NULL DEFAULT '{}'::jsonb,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key TEXT,
  blocked_reason TEXT,
  error JSONB,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_panel_runs_dedupe
  ON panel_runs(project_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_panel_runs_project
  ON panel_runs(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_studio_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_version_id TEXT NOT NULL REFERENCES agent_versions(id),
  panel_run_id TEXT REFERENCES panel_runs(id) ON DELETE CASCADE,
  member_key TEXT,
  task_id TEXT REFERENCES tasks(id),
  workflow_run_id TEXT REFERENCES workflow_runs(id),
  job_id TEXT REFERENCES jobs(id),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN (
      'queued', 'leased', 'running', 'waiting_decision', 'blocked',
      'completed', 'failed', 'deadlettered', 'cancelled'
    )),
  input JSONB NOT NULL DEFAULT '{}'::jsonb,
  compiled_context JSONB NOT NULL DEFAULT '{}'::jsonb,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  budget_reservation_id TEXT REFERENCES budget_reservations(id),
  receipt_id TEXT REFERENCES receipts(id),
  dedupe_key TEXT,
  error JSONB,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_studio_runs_dedupe
  ON agent_studio_runs(project_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_studio_runs_project
  ON agent_studio_runs(project_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_studio_runs_panel
  ON agent_studio_runs(panel_run_id, member_key);

CREATE TABLE IF NOT EXISTS panel_findings (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  panel_run_id TEXT NOT NULL REFERENCES panel_runs(id) ON DELETE CASCADE,
  agent_run_id TEXT NOT NULL REFERENCES agent_studio_runs(id) ON DELETE CASCADE,
  member_key TEXT NOT NULL,
  finding JSONB NOT NULL,
  recommendation JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0 CHECK (confidence >= 0 AND confidence <= 1),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_panel_findings_run
  ON panel_findings(panel_run_id, member_key, created_at);

CREATE TABLE IF NOT EXISTS panel_dissent (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  panel_run_id TEXT NOT NULL REFERENCES panel_runs(id) ON DELETE CASCADE,
  agent_run_id TEXT REFERENCES agent_studio_runs(id) ON DELETE SET NULL,
  member_key TEXT NOT NULL,
  dissent_kind TEXT NOT NULL DEFAULT 'minority_report',
  statement TEXT NOT NULL,
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'acknowledged', 'resolved', 'withdrawn')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_panel_dissent_run
  ON panel_dissent(panel_run_id, status, created_at);

-- The A2A ledger is deliberately normalized around immutable envelopes and
-- per-subscriber deliveries. Redis stream IDs are transport cursors only.
CREATE TABLE IF NOT EXISTS a2a_channels (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  channel_key TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  allowed_message_kinds JSONB NOT NULL DEFAULT '[]'::jsonb,
  allowed_schema_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
  retention_seconds INTEGER NOT NULL DEFAULT 604800 CHECK (retention_seconds > 0),
  max_payload_bytes INTEGER NOT NULL DEFAULT 65536 CHECK (max_payload_bytes > 0),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'paused', 'retired')),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, channel_key)
);
CREATE INDEX IF NOT EXISTS idx_a2a_channels_project
  ON a2a_channels(project_id, status, channel_key);

CREATE TABLE IF NOT EXISTS a2a_subscriptions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  channel_id TEXT NOT NULL REFERENCES a2a_channels(id) ON DELETE CASCADE,
  subscriber_kind TEXT NOT NULL
    CHECK (subscriber_kind IN ('agent', 'panel', 'workflow', 'worker', 'system', 'operator')),
  subscriber_id TEXT NOT NULL,
  consumer_group TEXT NOT NULL,
  recipient_filter JSONB NOT NULL DEFAULT '{}'::jsonb,
  ack_mode TEXT NOT NULL DEFAULT 'processed'
    CHECK (ack_mode IN ('none', 'accepted', 'processed')),
  visibility_timeout_seconds INTEGER NOT NULL DEFAULT 60
    CHECK (visibility_timeout_seconds BETWEEN 5 AND 3600),
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
  data_boundaries JSONB NOT NULL DEFAULT '["public","internal"]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'paused', 'revoked')),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(channel_id, consumer_group, subscriber_kind, subscriber_id)
);
CREATE INDEX IF NOT EXISTS idx_a2a_subscriptions_project
  ON a2a_subscriptions(project_id, channel_id, status);

CREATE TABLE IF NOT EXISTS a2a_envelopes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  channel_id TEXT NOT NULL REFERENCES a2a_channels(id),
  message_kind TEXT NOT NULL,
  schema_name TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
  sender_kind TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  sender_version_id TEXT,
  recipient JSONB NOT NULL,
  data_boundary TEXT NOT NULL DEFAULT 'internal',
  priority INTEGER NOT NULL DEFAULT 100,
  ttl_seconds INTEGER NOT NULL DEFAULT 3600 CHECK (ttl_seconds BETWEEN 1 AND 604800),
  not_before TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  ack_mode TEXT NOT NULL DEFAULT 'processed'
    CHECK (ack_mode IN ('none', 'accepted', 'processed')),
  conversation_id TEXT,
  correlation_id TEXT NOT NULL,
  causation_id TEXT REFERENCES a2a_envelopes(id),
  trace_id TEXT NOT NULL,
  reply_to_id TEXT REFERENCES a2a_envelopes(id),
  lineage JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload JSONB NOT NULL,
  payload_hash TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN (
      'draft', 'queued', 'dispatching', 'published', 'delivering',
      'acknowledged', 'partially_acknowledged', 'expired', 'cancelled', 'deadlettered'
    )),
  receipt_id TEXT REFERENCES receipts(id),
  redis_stream_id TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_a2a_envelopes_project
  ON a2a_envelopes(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_a2a_envelopes_correlation
  ON a2a_envelopes(project_id, correlation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_a2a_envelopes_conversation
  ON a2a_envelopes(project_id, conversation_id, created_at);

CREATE TABLE IF NOT EXISTS a2a_deliveries (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  envelope_id TEXT NOT NULL REFERENCES a2a_envelopes(id) ON DELETE CASCADE,
  subscription_id TEXT NOT NULL REFERENCES a2a_subscriptions(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN (
      'pending', 'published', 'claimed', 'processing', 'retry_wait',
      'acknowledged', 'rejected', 'deadlettered', 'expired', 'cancelled'
    )),
  attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  redis_stream_id TEXT,
  consumer_name TEXT,
  lease_owner TEXT,
  visibility_expires_at TIMESTAMPTZ,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error JSONB,
  claimed_at TIMESTAMPTZ,
  acknowledged_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(envelope_id, subscription_id)
);
CREATE INDEX IF NOT EXISTS idx_a2a_deliveries_claim
  ON a2a_deliveries(project_id, subscription_id, status, next_attempt_at, visibility_expires_at);

CREATE TABLE IF NOT EXISTS a2a_acknowledgements (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  delivery_id TEXT NOT NULL REFERENCES a2a_deliveries(id) ON DELETE CASCADE,
  acknowledgement TEXT NOT NULL
    CHECK (acknowledgement IN ('accepted', 'processed', 'rejected', 'deferred')),
  actor_kind TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  reason TEXT,
  receipt_id TEXT REFERENCES receipts(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE a2a_acknowledgements
  ADD COLUMN IF NOT EXISTS receipt_id TEXT REFERENCES receipts(id);
CREATE INDEX IF NOT EXISTS idx_a2a_acknowledgements_delivery
  ON a2a_acknowledgements(delivery_id, created_at);

CREATE TABLE IF NOT EXISTS a2a_transport_attempts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  envelope_id TEXT REFERENCES a2a_envelopes(id) ON DELETE CASCADE,
  delivery_id TEXT REFERENCES a2a_deliveries(id) ON DELETE CASCADE,
  operation TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
  attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
  redis_stream_id TEXT,
  consumer_group TEXT,
  latency_ms INTEGER,
  error JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_a2a_transport_attempts_project
  ON a2a_transport_attempts(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS a2a_outbox (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  envelope_id TEXT NOT NULL REFERENCES a2a_envelopes(id) ON DELETE CASCADE,
  job_id TEXT REFERENCES jobs(id),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'dispatching', 'published', 'retry_wait', 'deadlettered', 'cancelled')),
  attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(envelope_id)
);
CREATE INDEX IF NOT EXISTS idx_a2a_outbox_pending
  ON a2a_outbox(project_id, status, next_attempt_at);

-- ── AI SRE knowledge graph (RB-15) ────────────────────────────────────────────
-- A layered service/infra topology used to (a) build root-cause CONTEXT (the
-- subgraph around a failing service) and (b) capture LEARNING — confirmed causes
-- are written back as `caused_by` edges so future investigations start smarter.
-- `layer` = 'deterministic' (walked from k8s/cloud/infra_resources) or 'inferred'
-- (derived from traces/heuristics, carries a confidence < 1).
CREATE TABLE IF NOT EXISTS kg_nodes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL,               -- service|resource|repo|deployment|datastore|external|incident|root_cause
  node_key TEXT NOT NULL,           -- stable natural key within (project, kind)
  name TEXT NOT NULL,
  attrs JSONB NOT NULL DEFAULT '{}'::jsonb,
  layer TEXT NOT NULL DEFAULT 'deterministic'
    CHECK (layer IN ('deterministic', 'inferred')),
  source TEXT NOT NULL DEFAULT 'manual',  -- provenance source (manual|infra_resources|investigation|walk|…)
  confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
  valid_from TIMESTAMPTZ,           -- temporal validity window start (NULL = unbounded)
  valid_to TIMESTAMPTZ,             -- temporal validity window end (NULL = unbounded)
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, kind, node_key)
);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_project
  ON kg_nodes(project_id, kind, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_source
  ON kg_nodes(project_id, source);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_confidence
  ON kg_nodes(project_id, confidence);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_valid
  ON kg_nodes(project_id, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS kg_edges (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  src_id TEXT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
  dst_id TEXT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,               -- depends_on|runs_on|deployed_from|owns|emits|caused_by|correlates_with
  attrs JSONB NOT NULL DEFAULT '{}'::jsonb,
  layer TEXT NOT NULL DEFAULT 'deterministic'
    CHECK (layer IN ('deterministic', 'inferred')),
  source TEXT NOT NULL DEFAULT 'manual',
  confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
  valid_from TIMESTAMPTZ,
  valid_to TIMESTAMPTZ,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, src_id, dst_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_kg_edges_src ON kg_edges(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_kg_edges_dst ON kg_edges(dst_id, kind);

-- BOSS-357: knowledge retrieval evaluation snapshots (coverage + regression alarms)
CREATE TABLE IF NOT EXISTS kg_eval_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  coverage_score DOUBLE PRECISION NOT NULL,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  alarm BOOLEAN NOT NULL DEFAULT FALSE,
  threshold DOUBLE PRECISION NOT NULL DEFAULT 0.35,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kg_eval_snapshots_project
  ON kg_eval_snapshots(project_id, created_at DESC);

-- ── AI SRE reliability pipeline (RB-3/RB-4) ───────────────────────────────────
-- A telemetry source (a connected observability tool). Scan can also run against an
-- inline/ephemeral source, in which case anomaly_signals.source_id is NULL.
CREATE TABLE IF NOT EXISTS telemetry_sources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL,                       -- memory|log_export|grafana|splunk|datadog|...
  name TEXT NOT NULL,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  secret_ref TEXT,                          -- scoped read-only creds (never inlined)
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, kind, name)
);

-- One distilled anomaly signal from a scan (the mine() output, persisted).
CREATE TABLE IF NOT EXISTS anomaly_signals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  source_id TEXT REFERENCES telemetry_sources(id) ON DELETE SET NULL,
  incident_id TEXT,                         -- set once clustered
  kind TEXT NOT NULL,                       -- new_template|template_spike|error_surge|metric_spike
  signature TEXT NOT NULL,                  -- the template / metric name
  service TEXT NOT NULL DEFAULT '',
  score DOUBLE PRECISION,
  count INTEGER NOT NULL DEFAULT 0,
  sample JSONB NOT NULL DEFAULT '{}'::jsonb,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_anomaly_signals_incident ON anomaly_signals(incident_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_signals_project ON anomaly_signals(project_id, ts DESC);

-- A named incident: recurring signals with the same dedupe_key collapse into one.
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  dedupe_key TEXT NOT NULL,
  title TEXT NOT NULL,
  service TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'warning'
    CHECK (severity IN ('info', 'warning', 'critical')),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'triaged', 'diagnosing', 'remediating', 'resolved', 'dismissed')),
  confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  signal_count INTEGER NOT NULL DEFAULT 0,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  finalize_ctx JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_incidents_project ON incidents(project_id, status, last_seen DESC);

-- Evidence attached to an incident (templates, subgraph context, RCA hypotheses, traces).
CREATE TABLE IF NOT EXISTS incident_evidence (
  id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                       -- signal|rca_hypothesis|context|trace|remediation
  ref TEXT NOT NULL DEFAULT '',
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_incident_evidence_incident ON incident_evidence(incident_id, created_at);

-- ── Native LearningLoop primitive (RB-6) ──────────────────────────────────────
-- A BOSS-wide learning corpus: any agent appends labeled examples + outcomes; the
-- primitive computes metrics and suggests tuning. The SRE agent is the first heavy
-- consumer (incident feedback), generalizing the VAG verifier ratchet (#107).
CREATE TABLE IF NOT EXISTS learning_examples (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  agent TEXT NOT NULL,                       -- sre | verifier | ...
  kind TEXT NOT NULL,                        -- incident_feedback | remediation_outcome | ...
  label TEXT NOT NULL DEFAULT '',            -- verdict / class
  outcome TEXT NOT NULL DEFAULT '',          -- confirmed | dismissed | fix_good | fix_bad | ...
  ref TEXT NOT NULL DEFAULT '',              -- e.g. incident_id
  source TEXT NOT NULL DEFAULT 'manual',
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_learning_examples_project
  ON learning_examples(project_id, agent, kind, created_at DESC);

-- ── Remediation linkage (RB-7) ────────────────────────────────────────────────
-- Links a diagnosed incident to the governed code task that fixes it (and, on success,
-- the gated PR). The fix runs through the SAME governed pipeline as any code task —
-- design → tests → implement → review panel → security → fail-closed gate → PR.
CREATE TABLE IF NOT EXISTS remediations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  code_task_run_id TEXT,
  decision_id TEXT,
  status TEXT NOT NULL DEFAULT 'queued',     -- queued|running|blocked|completed|failed
  pull_request_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_remediations_incident ON remediations(incident_id, created_at DESC);

-- ── Reliability autonomy policy (RB-19) ───────────────────────────────────────
-- Governed autonomy spectrum per project: how far the SRE agent may go on its own.
-- Tier-0 auto_remediate: auto-launch the (still fail-closed, gated) remediation PR when
-- confidence/severity/blast-radius bounds hold. Tier-1: remediation only on operator request.
-- Tier-2: notify/handoff only. Every action remains receipted + gated by the pipeline.
CREATE TABLE IF NOT EXISTS reliability_policies (
  project_id TEXT PRIMARY KEY REFERENCES projects(id),
  auto_remediate BOOLEAN NOT NULL DEFAULT FALSE,
  min_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.8,
  max_blast_radius INTEGER NOT NULL DEFAULT 5,      -- subgraph size cap for autonomous action
  max_auto_severity TEXT NOT NULL DEFAULT 'critical'
    CHECK (max_auto_severity IN ('info', 'warning', 'critical')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Cost governance P1 (BOSS-280): per-agent attribution + cache-aware pricing ─
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS role TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_llm_calls_role ON llm_calls(project_id, role, created_at DESC);

-- ── BOSS-280 P1 backfill: attribute historical llm_calls to agent roles ───────
-- role is derived from stage; rows written before the column existed are backfilled once
-- (idempotent — only touches NULL roles), so $/agent covers history, not just new spend.
UPDATE llm_calls SET role = CASE
    WHEN stage IN ('standards','spec','plan','review_plan','design') THEN 'architect'
    WHEN stage = 'write_tests' THEN 'qa'
    WHEN stage IN ('check_tests','run_all_tests') THEN 'qa'
    WHEN stage IN ('implement','repair','guest','solo') THEN 'software_engineer'
    WHEN stage IN ('review_code','consistency_check') THEN 'reviewer'
    WHEN stage = 'security_check' THEN 'security_reviewer'
    ELSE NULL END
WHERE role IS NULL AND stage IS NOT NULL;

-- Advisory gate (nullable on purpose). NULL means "use the preset's value", so adding this
-- column cannot silently redefine a preset that predates it. Lean sets it true: the adversarial
-- panel still runs and still records every verdict, but a reviewer's opinion cannot withhold
-- the PR + preview. Regressions are exempt (_ADVISORY_HARD_FLOOR).
ALTER TABLE pipeline_profiles ADD COLUMN IF NOT EXISTS advisory_gate BOOLEAN;
-- Which adversarial seats sit on the panel (NULL -> the preset's list -> the full panel).
-- `lean` narrows this to ["regression"]: tests are still written first and the suite still
-- runs, but the three LLM seats that argue about the code are not convened.
ALTER TABLE pipeline_profiles ADD COLUMN IF NOT EXISTS panel_checks JSONB;

-- FENCING FOR RUN EXECUTION. A job lease only means something if the holder stops when it
-- loses it. Nothing enforced that, so a reclaimed job ran in two workers at once and both
-- wrote this table (phase moved backwards; stages re-ran; 89 min and $4.44 for no code).
-- owner_token is the sha256 of the executing worker's lease token: a reclaiming worker
-- overwrites it and thereby fences the previous executor, whose writes then match 0 rows.
-- state_version makes contention visible after the fact — it jumps in bursts under a race.
ALTER TABLE code_task_runs ADD COLUMN IF NOT EXISTS owner_token TEXT;
ALTER TABLE code_task_runs ADD COLUMN IF NOT EXISTS state_version INTEGER NOT NULL DEFAULT 0;

-- ===========================================================================
-- TASK SHEPHERD (docs/product/TASK_SHEPHERD_DESIGN.md)
-- An internal AI developer that owns a task from "asked for" to "merged, or a
-- human has been asked one clear question". Grounded in the 2026-08-03/04
-- session where 58% of spend ($38.37 of $66.65) went to runs producing nothing,
-- all of it operational rather than coding failure.
-- ===========================================================================

-- What has been tried for a task, and what remains. THIS is what bounds the
-- shepherd: a remedy may be used once per task, so termination is a property of
-- the ledger rather than of the model's judgement.
CREATE TABLE IF NOT EXISTS shepherd_ledger (
  id             TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_ref       TEXT NOT NULL,              -- crun_... the shepherd is shepherding
  failure_class  INTEGER NOT NULL,
  remedy_id      TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'available',  -- available|used|blocked
  used_at        TIMESTAMPTZ,
  cost_usd_micros BIGINT NOT NULL DEFAULT 0,
  outcome        TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (task_ref, failure_class, remedy_id)
);
CREATE INDEX IF NOT EXISTS idx_shepherd_ledger_task ON shepherd_ledger(task_ref, status);

-- The reasoning itself — the primary artifact, and what the console renders.
-- predicted_* and actual_* are both stored so "is it actually reasoning?" is a
-- measurable question (calibration) rather than an impression.
CREATE TABLE IF NOT EXISTS shepherd_reasoning (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_ref          TEXT NOT NULL,
  step_no           INTEGER NOT NULL,
  observe           TEXT NOT NULL,
  signals           JSONB NOT NULL DEFAULT '[]'::jsonb,
  hypothesis        TEXT,
  confidence        NUMERIC(3,2),
  check_result      TEXT,                    -- a hypothesis must be CHECKED, not asserted
  check_confirmed   BOOLEAN,
  failure_class     INTEGER,
  remedy_chosen     TEXT,
  rationale         TEXT,
  predicted_cost_usd_micros BIGINT,
  predicted_outcome TEXT,
  actual_cost_usd_micros    BIGINT,
  actual_outcome    TEXT,
  stopped_because   TEXT,                    -- success|ledger|loop|budget|human|deadline
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (task_ref, step_no)
);
CREATE INDEX IF NOT EXISTS idx_shepherd_reasoning_task ON shepherd_reasoning(task_ref, step_no);

-- Every judgement the in-flight shepherd makes, whether or not it acted on it.
--
-- Exists so a capability held back for safety cannot quietly rot. Shadow mode without a
-- ledger is a promise to review something later that nobody reviews: the flag stays off, the
-- evidence never accumulates anywhere visible, and re-enabling it months later reintroduces
-- exactly the bugs it was switched off for. Recording every verdict makes readiness a
-- MEASUREMENT — "this class judged 40 runs and was never overruled" — instead of a memory.
--
-- `mode` is what the shepherd was allowed to do at the time, not what it wanted to do:
-- shadow means it recorded and stood back; live means it acted. Keeping both in one table is
-- what makes "would this have been right?" answerable after the fact.
CREATE TABLE IF NOT EXISTS shepherd_watch_observations (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id        TEXT NOT NULL,
  watch_class   INTEGER NOT NULL,
  class_name    TEXT NOT NULL,
  action        TEXT NOT NULL,               -- none|cancel|requeue|fail|escalate
  mode          TEXT NOT NULL,               -- shadow|live
  acted         BOOLEAN NOT NULL DEFAULT FALSE,
  reason        TEXT NOT NULL,
  evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Filled in later by a human or by the run's own outcome: was the shepherd right?
  -- NULL means nobody has judged it yet, which is different from "it was fine".
  verdict_correct BOOLEAN,
  judged_by     TEXT,
  judged_at     TIMESTAMPTZ,
  note          TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shepherd_watch_obs_class
  ON shepherd_watch_observations(project_id, watch_class, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shepherd_watch_obs_run
  ON shepherd_watch_observations(run_id, created_at DESC);

-- One decision-shaped question for a human. Never a log dump: options are
-- concrete and each carries its cost.
CREATE TABLE IF NOT EXISTS shepherd_escalations (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_ref     TEXT NOT NULL,
  question     TEXT NOT NULL,
  options      JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence     JSONB NOT NULL DEFAULT '{}'::jsonb,
  status       TEXT NOT NULL DEFAULT 'open',   -- open|answered|withdrawn
  answered_by  TEXT,
  answer       TEXT,
  answered_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shepherd_escalations_open ON shepherd_escalations(project_id, status);

-- Why a shepherd was told to wait. Cross-task interference is the coordinator's
-- job: merging PR #66 made #67 and #68 CONFLICTING instantly, which no per-task
-- shepherd could have seen.
CREATE TABLE IF NOT EXISTS coordinator_rulings (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  subject     JSONB NOT NULL DEFAULT '[]'::jsonb,   -- task refs the ruling covers
  ruling      TEXT NOT NULL,                        -- proceed|hold|sequence|block
  rationale   TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- GROUND-TRUTH AND ASSURANCE RUNTIME (BOSS-123..128, BOSS-131)
-- These records move measured truth, settled conclusions, evidence, claims,
-- and enforcement decisions out of model memory and into the durable plane.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ground_truth_metric_definitions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  metric_key TEXT NOT NULL,
  version INTEGER NOT NULL CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('draft', 'active', 'superseded', 'retired')),
  metric_kind TEXT NOT NULL
    CHECK (metric_kind IN ('coverage', 'freshness', 'correctness', 'completeness', 'custom')),
  unit TEXT NOT NULL DEFAULT 'count',
  comparator TEXT NOT NULL DEFAULT 'gte'
    CHECK (comparator IN ('gte', 'lte', 'eq', 'between')),
  baseline DOUBLE PRECISION NOT NULL,
  threshold DOUBLE PRECISION NOT NULL,
  threshold_upper DOUBLE PRECISION,
  cadence_seconds INTEGER NOT NULL CHECK (cadence_seconds > 0),
  alert_sla_seconds INTEGER NOT NULL CHECK (alert_sla_seconds > 0),
  owner_ref TEXT NOT NULL,
  evidence_query JSONB NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, metric_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ground_truth_metric_current
  ON ground_truth_metric_definitions(project_id, metric_key)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_ground_truth_metrics_project
  ON ground_truth_metric_definitions(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS ground_truth_observations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  metric_definition_id TEXT NOT NULL REFERENCES ground_truth_metric_definitions(id),
  metric_definition_version INTEGER NOT NULL CHECK (metric_definition_version > 0),
  value DOUBLE PRECISION NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('healthy', 'breached', 'inconclusive')),
  evaluator_job_id TEXT NOT NULL REFERENCES jobs(id),
  evaluator_worker_id TEXT NOT NULL,
  lease_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence JSONB NOT NULL,
  dedupe_key TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_ground_truth_observations_metric
  ON ground_truth_observations(project_id, metric_definition_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS regression_signals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  metric_definition_id TEXT NOT NULL REFERENCES ground_truth_metric_definitions(id),
  first_observation_id TEXT NOT NULL REFERENCES ground_truth_observations(id),
  latest_observation_id TEXT NOT NULL REFERENCES ground_truth_observations(id),
  decision_id TEXT REFERENCES decisions(id),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'acknowledged', 'recovered', 'dismissed')),
  dedupe_key TEXT NOT NULL,
  breach_started_at TIMESTAMPTZ NOT NULL,
  recovered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_regression_signals_open
  ON regression_signals(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS settled_conclusions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  conclusion_key TEXT NOT NULL,
  version INTEGER NOT NULL CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'current'
    CHECK (status IN ('draft', 'current', 'superseded', 'reopened', 'expired', 'retired')),
  conclusion TEXT NOT NULL,
  scope JSONB NOT NULL DEFAULT '{}'::jsonb,
  owner_ref TEXT NOT NULL,
  tell_patterns JSONB NOT NULL,
  cause_menu JSONB NOT NULL,
  evidence_refs JSONB NOT NULL,
  reopen_conditions JSONB NOT NULL DEFAULT '[]'::jsonb,
  effective_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  review_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, conclusion_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_settled_conclusion_current
  ON settled_conclusions(project_id, conclusion_key)
  WHERE status = 'current';
CREATE INDEX IF NOT EXISTS idx_settled_conclusions_scope
  ON settled_conclusions(project_id, status, effective_at DESC);

CREATE TABLE IF NOT EXISTS closed_question_matches (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  settled_conclusion_id TEXT NOT NULL REFERENCES settled_conclusions(id),
  settled_conclusion_version INTEGER NOT NULL CHECK (settled_conclusion_version > 0),
  publication_boundary TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  matched_patterns JSONB NOT NULL,
  context_classification TEXT NOT NULL
    CHECK (context_classification IN ('actionable', 'quoted', 'historical', 'negated')),
  status TEXT NOT NULL CHECK (status IN ('blocked', 'allowed_context', 'overridden')),
  stop_response JSONB NOT NULL DEFAULT '{}'::jsonb,
  decision_id TEXT REFERENCES decisions(id),
  dedupe_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_closed_question_matches_project
  ON closed_question_matches(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS assurance_cases (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  subject_type TEXT NOT NULL,
  subject_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  risk_tier INTEGER NOT NULL DEFAULT 1 CHECK (risk_tier BETWEEN 0 AND 3),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'evaluating', 'accepted', 'blocked', 'refuted', 'inconclusive', 'reopened')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  metric_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  settled_conclusion_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  surface_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  agent_group_run_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  workflow_run_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  skill_invocation_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  integration_connection_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  decision_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  final_reason JSONB NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_assurance_cases_project
  ON assurance_cases(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS assurance_policy_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  assurance_case_id TEXT NOT NULL REFERENCES assurance_cases(id) ON DELETE CASCADE,
  assurance_case_version INTEGER NOT NULL,
  org_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
  project_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
  effective_rules JSONB NOT NULL,
  conflicts JSONB NOT NULL DEFAULT '[]'::jsonb,
  content_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(assurance_case_id, assurance_case_version, content_hash)
);

CREATE TABLE IF NOT EXISTS assurance_claims (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  assurance_case_id TEXT NOT NULL REFERENCES assurance_cases(id) ON DELETE CASCADE,
  claim_type TEXT NOT NULL
    CHECK (claim_type IN ('solved', 'fixed', 'regressed', 'caused_by', 'healthy', 'status')),
  statement TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('draft', 'pending', 'authoritative', 'blocked', 'inconclusive', 'refuted', 'invalidated')),
  evidence_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
  producer_ref TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_assurance_claims_case
  ON assurance_claims(assurance_case_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS assurance_evidence (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  assurance_case_id TEXT NOT NULL REFERENCES assurance_cases(id) ON DELETE CASCADE,
  claim_id TEXT REFERENCES assurance_claims(id) ON DELETE CASCADE,
  evidence_type TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  uri TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  provenance JSONB NOT NULL,
  reproduction JSONB NOT NULL,
  produced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until TIMESTAMPTZ,
  invalidated_at TIMESTAMPTZ,
  invalidation_reason TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, claim_id, content_hash, evidence_type)
);
CREATE INDEX IF NOT EXISTS idx_assurance_evidence_claim
  ON assurance_evidence(project_id, claim_id, created_at DESC);

CREATE TABLE IF NOT EXISTS claim_verifications (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  assurance_case_id TEXT NOT NULL REFERENCES assurance_cases(id) ON DELETE CASCADE,
  claim_id TEXT NOT NULL REFERENCES assurance_claims(id) ON DELETE CASCADE,
  claim_version INTEGER NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('passed', 'refuted', 'inconclusive')),
  reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  policy_snapshot_id TEXT REFERENCES assurance_policy_snapshots(id),
  dedupe_key TEXT NOT NULL,
  verified_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_claim_verifications_claim
  ON claim_verifications(claim_id, created_at DESC);

CREATE TABLE IF NOT EXISTS surface_ownerships (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  surface_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  owner_ref TEXT NOT NULL,
  coordination_route TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'transferring', 'relinquished', 'expired')),
  expires_at TIMESTAMPTZ,
  acquired_by TEXT NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, surface_key, environment)
);
CREATE INDEX IF NOT EXISTS idx_surface_ownerships_project
  ON surface_ownerships(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS ownership_transitions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  surface_ownership_id TEXT NOT NULL REFERENCES surface_ownerships(id) ON DELETE CASCADE,
  transition_kind TEXT NOT NULL
    CHECK (transition_kind IN ('acquire', 'transfer', 'relinquish', 'expire', 'emergency_takeover', 'preflight')),
  from_owner_ref TEXT,
  to_owner_ref TEXT,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  decision_id TEXT REFERENCES decisions(id),
  actor TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_ownership_transitions_surface
  ON ownership_transitions(surface_ownership_id, created_at DESC);

CREATE TABLE IF NOT EXISTS verification_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  policy_key TEXT NOT NULL,
  version INTEGER NOT NULL CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('draft', 'active', 'superseded', 'retired')),
  surface_patterns JSONB NOT NULL DEFAULT '[]'::jsonb,
  claim_types JSONB NOT NULL DEFAULT '[]'::jsonb,
  risk_tiers JSONB NOT NULL DEFAULT '[]'::jsonb,
  cadence_seconds INTEGER NOT NULL CHECK (cadence_seconds > 0),
  sample_rate DOUBLE PRECISION NOT NULL DEFAULT 1 CHECK (sample_rate >= 0 AND sample_rate <= 1),
  verification_method JSONB NOT NULL,
  independence_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  escalation_target TEXT NOT NULL,
  estimated_cost_usd_micros BIGINT NOT NULL DEFAULT 0 CHECK (estimated_cost_usd_micros >= 0),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, policy_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_verification_policy_current
  ON verification_policies(project_id, policy_key) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS verification_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  assurance_case_id TEXT NOT NULL REFERENCES assurance_cases(id) ON DELETE CASCADE,
  claim_id TEXT NOT NULL REFERENCES assurance_claims(id) ON DELETE CASCADE,
  verification_policy_id TEXT NOT NULL REFERENCES verification_policies(id),
  job_id TEXT REFERENCES jobs(id),
  budget_reservation_id TEXT REFERENCES budget_reservations(id),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'leased', 'running', 'passed', 'refuted', 'inconclusive', 'failed', 'deadlettered', 'cancelled')),
  producer_path JSONB NOT NULL DEFAULT '{}'::jsonb,
  verifier_path JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_bundle JSONB NOT NULL DEFAULT '[]'::jsonb,
  receipt_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key TEXT NOT NULL,
  scheduled_for TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_verification_runs_queue
  ON verification_runs(project_id, status, scheduled_for);

-- BOSS-130: typed integration definitions, tenant-scoped connections, and the
-- durable explanation of every capability resolution decision.
CREATE TABLE IF NOT EXISTS integration_definitions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  integration_key TEXT NOT NULL,
  version INTEGER NOT NULL CHECK (version > 0),
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('draft', 'active', 'superseded', 'retired')),
  skill_contracts JSONB NOT NULL,
  executor_bindings JSONB NOT NULL DEFAULT '[]'::jsonb,
  provenance JSONB NOT NULL,
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, integration_key, version),
  UNIQUE(project_id, content_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_definition_current
  ON integration_definitions(project_id, integration_key) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS integration_connections (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  integration_definition_id TEXT NOT NULL REFERENCES integration_definitions(id),
  connection_key TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('org', 'workspace', 'project')),
  scope_id TEXT NOT NULL,
  owner_ref TEXT NOT NULL,
  secret_ref_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  granted_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  data_boundaries JSONB NOT NULL DEFAULT '[]'::jsonb,
  max_risk_tier INTEGER NOT NULL DEFAULT 1 CHECK (max_risk_tier BETWEEN 0 AND 3),
  health_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (health_status IN ('pending', 'healthy', 'degraded', 'stale', 'failed', 'revoked')),
  health_checked_at TIMESTAMPTZ,
  freshness_seconds INTEGER NOT NULL DEFAULT 3600 CHECK (freshness_seconds > 0),
  rate_limit_state JSONB NOT NULL DEFAULT '{}'::jsonb,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'active', 'disabled', 'revoked')),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, connection_key)
);
CREATE INDEX IF NOT EXISTS idx_integration_connections_project
  ON integration_connections(project_id, status, health_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS capability_resolutions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  integration_connection_id TEXT REFERENCES integration_connections(id),
  skill_id TEXT NOT NULL,
  actor_ref TEXT NOT NULL,
  workflow_step_ref TEXT,
  agent_group_version_ref TEXT,
  requested_data_boundary TEXT NOT NULL,
  risk_tier INTEGER NOT NULL CHECK (risk_tier BETWEEN 0 AND 3),
  authority_inputs JSONB NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('allowed', 'denied')),
  denial_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
  resolved_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_capability_resolutions_project
  ON capability_resolutions(project_id, created_at DESC);

-- BOSS-129: reusable collaboration protocols. Panels may reference this
-- runtime, but consensus remains advisory unless evidence/policy gates pass.
CREATE TABLE IF NOT EXISTS agent_group_specs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  group_key TEXT NOT NULL,
  name TEXT NOT NULL,
  purpose TEXT NOT NULL,
  owner_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'active', 'retired')),
  active_version_id TEXT,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, group_key)
);

CREATE TABLE IF NOT EXISTS agent_group_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_group_spec_id TEXT NOT NULL REFERENCES agent_group_specs(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'published', 'retired')),
  topology TEXT NOT NULL
    CHECK (topology IN ('panel', 'producer_reviewer', 'bounded_debate', 'swarm', 'fan_out', 'sequential_handoff')),
  members JSONB NOT NULL,
  collaboration_protocol JSONB NOT NULL,
  allowed_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  context_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  independence_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  bounds JSONB NOT NULL,
  completion_rule JSONB NOT NULL,
  escalation_target TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  UNIQUE(agent_group_spec_id, version),
  UNIQUE(agent_group_spec_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_agent_group_versions_project
  ON agent_group_versions(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_group_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_group_version_id TEXT NOT NULL REFERENCES agent_group_versions(id),
  assurance_case_id TEXT REFERENCES assurance_cases(id),
  workflow_run_id TEXT REFERENCES workflow_runs(id),
  task_id TEXT REFERENCES tasks(id),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'waiting', 'blocked', 'completed', 'failed', 'deadlettered', 'cancelled')),
  input JSONB NOT NULL,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  policy_snapshot JSONB NOT NULL,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  dissent JSONB NOT NULL DEFAULT '[]'::jsonb,
  total_cost_usd_micros BIGINT NOT NULL DEFAULT 0,
  budget_reservation_id TEXT REFERENCES budget_reservations(id),
  receipt_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  dedupe_key TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deadline_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_group_runs_project
  ON agent_group_runs(project_id, status, updated_at DESC);
ALTER TABLE agent_group_runs ADD COLUMN IF NOT EXISTS budget_reservation_id TEXT REFERENCES budget_reservations(id);
ALTER TABLE agent_group_runs ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS agent_group_member_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_group_run_id TEXT NOT NULL REFERENCES agent_group_runs(id) ON DELETE CASCADE,
  member_key TEXT NOT NULL,
  role_key TEXT NOT NULL,
  job_id TEXT REFERENCES jobs(id),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'leased', 'running', 'completed', 'failed', 'blocked', 'timed_out', 'deadlettered', 'cancelled')),
  independent BOOLEAN NOT NULL DEFAULT true,
  input JSONB NOT NULL DEFAULT '{}'::jsonb,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
  messages JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  receipt_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  cost_usd_micros BIGINT NOT NULL DEFAULT 0,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(agent_group_run_id, member_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_group_member_runs_group
  ON agent_group_member_runs(agent_group_run_id, status, created_at);

-- Durable Worker Fabric inventory. Registration is informational; leases remain
-- the execution fence in jobs and cannot be granted by this registry.
CREATE TABLE IF NOT EXISTS worker_registrations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  worker_id TEXT NOT NULL,
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'offline', 'draining')),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, worker_id)
);
CREATE INDEX IF NOT EXISTS idx_worker_registrations_liveness
  ON worker_registrations(project_id, status, last_seen_at DESC);

-- ---------------------------------------------------------------------------
-- Goldeneye bare-metal PaaS (BOSS control plane)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS goldeneye_nodes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  display_name TEXT,
  roles JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- control | edge | apps | data | mixed
  status TEXT NOT NULL DEFAULT 'registered',
  -- registered | bootstrapping | ready | degraded | offline | retired
  public_ip TEXT,
  tailnet_ip TEXT,
  ssh_host TEXT,
  ssh_user TEXT NOT NULL DEFAULT 'root',
  ssh_port INTEGER NOT NULL DEFAULT 22,
  kit_version TEXT,
  site_config JSONB NOT NULL DEFAULT '{}'::jsonb,
  labels JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_heartbeat_at TIMESTAMPTZ,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_goldeneye_nodes_project
  ON goldeneye_nodes(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS goldeneye_package_installs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES goldeneye_nodes(id) ON DELETE CASCADE,
  package_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  -- planned | installing | installed | simulated | failed | removed
  layer INTEGER,
  mode TEXT NOT NULL DEFAULT 'simulated',
  -- simulated | real
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_goldeneye_pkg_installs_node
  ON goldeneye_package_installs(project_id, node_id, package_id, created_at DESC);

CREATE TABLE IF NOT EXISTS goldeneye_apps (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES goldeneye_nodes(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  -- planned | deploying | running | stopped | simulated | failed | removed
  mode TEXT NOT NULL DEFAULT 'image',
  -- image | compose
  image_ref TEXT,
  previous_image_ref TEXT,
  compose_ref TEXT,
  domain TEXT,
  public_url TEXT,
  admin_url TEXT,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_goldeneye_apps_project
  ON goldeneye_apps(project_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_goldeneye_apps_node
  ON goldeneye_apps(project_id, node_id, status);

CREATE TABLE IF NOT EXISTS goldeneye_deploys (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  app_id TEXT NOT NULL REFERENCES goldeneye_apps(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES goldeneye_nodes(id) ON DELETE CASCADE,
  action TEXT NOT NULL DEFAULT 'deploy',
  -- deploy | rollback | stop | start | remove
  status TEXT NOT NULL DEFAULT 'planned',
  -- planned | running | succeeded | simulated | failed
  image_ref TEXT,
  previous_image_ref TEXT,
  mode TEXT NOT NULL DEFAULT 'simulated',
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_goldeneye_deploys_app
  ON goldeneye_deploys(project_id, app_id, created_at DESC);

-- Fail-closed: evaluations with no source data use status=unknown (never silent ok).
CREATE TABLE IF NOT EXISTS provider_slo_definitions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider TEXT NOT NULL,
  provider_account_id TEXT REFERENCES provider_accounts(id),
  metric_key TEXT NOT NULL,
  -- availability | healthy_ratio | api_available | cost_usd_micros | sample_count
  target DOUBLE PRECISION NOT NULL,
  window_seconds INTEGER NOT NULL DEFAULT 3600,
  comparator TEXT NOT NULL DEFAULT 'gte',
  -- gte | lte | gt | lt | eq
  status TEXT NOT NULL DEFAULT 'unknown',
  -- cached last evaluation: ok | breached | unknown
  last_value DOUBLE PRECISION,
  evaluated_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Expression unique index so NULL provider_account_id still dedupes correctly.
CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_slo_definitions_key
  ON provider_slo_definitions (
    project_id,
    provider,
    COALESCE(provider_account_id, ''),
    metric_key,
    window_seconds
  );
CREATE INDEX IF NOT EXISTS idx_provider_slo_definitions_project
  ON provider_slo_definitions(project_id, provider, status);

CREATE TABLE IF NOT EXISTS provider_slo_evaluations (
  id TEXT PRIMARY KEY,
  definition_id TEXT NOT NULL REFERENCES provider_slo_definitions(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id),
  provider TEXT NOT NULL,
  provider_account_id TEXT REFERENCES provider_accounts(id),
  metric_key TEXT NOT NULL,
  target DOUBLE PRECISION NOT NULL,
  window_seconds INTEGER NOT NULL,
  comparator TEXT NOT NULL,
  status TEXT NOT NULL,
  -- ok | breached | unknown
  last_value DOUBLE PRECISION,
  evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_provider_slo_evaluations_def
  ON provider_slo_evaluations(definition_id, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_provider_slo_evaluations_project
  ON provider_slo_evaluations(project_id, provider, status, evaluated_at DESC);

-- Platform ops: on-call SRE
CREATE TABLE IF NOT EXISTS oncall_schedules (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  layers JSONB NOT NULL DEFAULT '[]'::jsonb,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_oncall_schedules_project
  ON oncall_schedules(project_id, enabled);

CREATE TABLE IF NOT EXISTS oncall_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  schedule_id TEXT,
  steps JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_oncall_policies_project
  ON oncall_policies(project_id);

CREATE TABLE IF NOT EXISTS oncall_pages (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  reliability_incident_id TEXT,
  dedupe_key TEXT NOT NULL,
  title TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'warning',
  status TEXT NOT NULL DEFAULT 'open',
  schedule_id TEXT,
  policy_id TEXT,
  current_step INTEGER NOT NULL DEFAULT 0,
  assignee TEXT NOT NULL DEFAULT '',
  service TEXT NOT NULL DEFAULT '',
  kg_node_id TEXT,
  kg_node_key TEXT,
  adapter TEXT NOT NULL DEFAULT 'slack_webhook',
  notify_result JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  acked_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ,
  UNIQUE (project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_oncall_pages_project
  ON oncall_pages(project_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_oncall_pages_incident
  ON oncall_pages(project_id, reliability_incident_id);


-- Platform ops: release planning
CREATE TABLE IF NOT EXISTS release_plans (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  target_date DATE,
  status TEXT NOT NULL DEFAULT 'planned',
  item_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  issue_links JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_release_plans_project
  ON release_plans(project_id, target_date NULLS LAST, created_at DESC);

CREATE TABLE IF NOT EXISTS release_board_items (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  column_key TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT,
  external_url TEXT,
  tracker TEXT,
  issue_key TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, source_type, source_id),
  CHECK (column_key IN ('built', 'building', 'planned', 'blocked'))
);
CREATE INDEX IF NOT EXISTS idx_release_board_items_project_col
  ON release_board_items(project_id, column_key, sort_order, updated_at DESC);


-- Platform ops: worker autoscaling
CREATE TABLE IF NOT EXISTS worker_autoscaling_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  pool_key TEXT NOT NULL DEFAULT 'default',
  enabled BOOLEAN NOT NULL DEFAULT true,
  min_workers INTEGER NOT NULL DEFAULT 0,
  max_workers INTEGER NOT NULL DEFAULT 8,
  scale_up_threshold INTEGER NOT NULL DEFAULT 1,
  cooldown_seconds INTEGER NOT NULL DEFAULT 30,
  max_queue_depth_per_worker INTEGER NOT NULL DEFAULT 5,
  kind TEXT NOT NULL DEFAULT 'local',
  provider_account_id TEXT REFERENCES provider_accounts(id) ON DELETE SET NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_applied_at TIMESTAMPTZ,
  last_scale_direction TEXT,
  created_by TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, pool_key)
);
CREATE INDEX IF NOT EXISTS idx_worker_autoscaling_policies_project
  ON worker_autoscaling_policies(project_id, enabled, pool_key);

CREATE TABLE IF NOT EXISTS worker_autoscaling_history (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  pool_key TEXT NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  queue_depth INTEGER NOT NULL DEFAULT 0,
  leased_jobs INTEGER NOT NULL DEFAULT 0,
  running_jobs INTEGER NOT NULL DEFAULT 0,
  desired_workers INTEGER NOT NULL DEFAULT 0,
  actual_workers INTEGER NOT NULL DEFAULT 0,
  previous_desired INTEGER NOT NULL DEFAULT 0,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  elastic_result JSONB NOT NULL DEFAULT '{}'::jsonb,
  reason TEXT,
  actor TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_worker_autoscaling_history_project
  ON worker_autoscaling_history(project_id, pool_key, created_at DESC);


-- Platform ops: token efficiency policies
CREATE TABLE IF NOT EXISTS token_budget_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT '*',
  max_prompt_tokens INTEGER,
  max_completion_tokens INTEGER,
  max_total_tokens_per_call INTEGER,
  max_tokens_per_day BIGINT,
  max_cost_usd_micros_per_day BIGINT,
  alert_at_pct NUMERIC NOT NULL DEFAULT 0.80,
  preferred_model_class TEXT,
  enabled BOOLEAN NOT NULL DEFAULT true,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, stage),
  CHECK (preferred_model_class IS NULL
         OR preferred_model_class IN ('low_cost', 'strong')),
  CHECK (alert_at_pct > 0 AND alert_at_pct <= 1)
);
CREATE INDEX IF NOT EXISTS idx_token_budget_policies_project
  ON token_budget_policies (project_id, stage);


-- BOSS-11: durable persistent preview sandbox reservations (Workload B)
CREATE TABLE IF NOT EXISTS preview_sandboxes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  app_id TEXT NOT NULL,
  run_id TEXT,
  repo TEXT,
  branch TEXT,
  image_or_build TEXT,
  run_cmd TEXT,
  env_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  port INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  host_ref TEXT,
  url TEXT,
  internal_url TEXT,
  error TEXT,
  simulate BOOLEAN NOT NULL DEFAULT false,
  ttl_seconds INTEGER NOT NULL DEFAULT 3600,
  expires_at TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ,
  reserved_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, app_id)
);
CREATE INDEX IF NOT EXISTS idx_preview_sandboxes_project
  ON preview_sandboxes (project_id, created_at DESC);

-- Agent KMS (agent ownership, grants, leases)
CREATE TABLE IF NOT EXISTS agent_key_entries (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  secret_ref_id TEXT NOT NULL,
  owner_agent TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'general',
  labels JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_agent_key_entries_project
  ON agent_key_entries(project_id, owner_agent, status);

CREATE TABLE IF NOT EXISTS agent_key_grants (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  key_entry_id TEXT NOT NULL REFERENCES agent_key_entries(id) ON DELETE CASCADE,
  grantee_agent TEXT NOT NULL,
  permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  granted_by TEXT NOT NULL,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (key_entry_id, grantee_agent)
);
CREATE INDEX IF NOT EXISTS idx_agent_key_grants_grantee
  ON agent_key_grants(project_id, grantee_agent);

CREATE TABLE IF NOT EXISTS agent_key_leases (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  key_entry_id TEXT NOT NULL,
  secret_ref_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT '',
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  reveal_count INT NOT NULL DEFAULT 0,
  max_reveals INT NOT NULL DEFAULT 1,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_key_leases_agent
  ON agent_key_leases(project_id, agent_id, expires_at);
