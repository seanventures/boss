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
  policy_notes JSONB NOT NULL DEFAULT '[]'::jsonb,
  observed_capacity JSONB NOT NULL DEFAULT '{}'::jsonb,
  runtime_supported BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, source_id)
);

CREATE TABLE IF NOT EXISTS routing_logs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  skill_id TEXT,
  task_kind TEXT NOT NULL,
  workload_class TEXT NOT NULL,
  risk_tier INTEGER NOT NULL,
  budget_state TEXT NOT NULL,
  selected_source_id TEXT,
  selected_provider TEXT,
  selected_model_id TEXT,
  denial_reason TEXT,
  rejected_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
  decision JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  owner_role_id TEXT,
  priority TEXT NOT NULL DEFAULT 'p2',
  risk_tier INTEGER NOT NULL DEFAULT 0,
  inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
  required_artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
  budget_allocation_id TEXT,
  dedupe_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);

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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(org_id, dedupe_key)
);

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
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  amount_usd_micros BIGINT NOT NULL,
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
  dedupe_key TEXT NOT NULL,
  receipt_id TEXT REFERENCES receipts(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS inbox_messages (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  channel TEXT NOT NULL,
  raw_ref TEXT,
  subject TEXT,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'received',
  parsed JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deadletters (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
