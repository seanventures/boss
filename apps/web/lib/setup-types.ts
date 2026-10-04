/**
 * Repo and models: the frozen contract with GET /v1/setup (apps/api/app/schemas_setup.py).
 *
 * The server decides every pill — label, tone, tooltip, action — so the console only renders.
 * Nothing in apps/web computes a tone from a state (tests/setup-renders-server-pills.test.tsx).
 */

export type Tone = "green" | "amber" | "red" | "grey" | "busy";
export type PillAction = "connect" | "test" | "replace" | "fix_link" | "budget" | "default_model";

export type Pill = {
  state: string;
  tone: Tone;
  label: string;
  tip: string;
  action?: PillAction | null;
  fix_url?: string | null;
};

export type CheckResult = { ok: boolean | null; state: string; at: string | null };

export type RepoEntry = {
  binding_id: string | null;
  full_name: string;
  default: boolean;
  private: boolean | null;
  default_branch: string | null;
  checks: { read: CheckResult; write: CheckResult; pull_requests: CheckResult };
  pill: Pill;
  checked_at: string | null;
  stale?: boolean;
};

export type RepoConnection = {
  id: string;
  auth: "token" | "app";
  account_login: string | null;
  expires_at: string | null;
  source: "connected" | "install";
};

export type ModelKind = "subscription" | "metered" | "local";

export type ModelEntry = {
  source_id: string;
  provider: string;
  label: string;
  kind: ModelKind;
  model: string | null;
  used_for: string[];
  pill: Pill;
  checked_at: string | null;
  source: "connected" | "install";
  retry_at: string | null;
  stale?: boolean;
};

export type Found = "signed_in" | "needs_token" | "needs_file" | "not_on_server" | "found";

export type Detected = {
  provider: string;
  label: string;
  kind: "subscription" | "local";
  found: Found;
  base_url?: string | null;
};

export type Field = "api_key" | "token" | "file" | "base_url" | "model";

export type CatalogueEntry = {
  provider: string;
  label: string;
  kind: "subscription" | "api_key" | "local";
  key_url?: string | null;
  example_model?: string | null;
  fields: Field[];
  command?: string | null;
};

export type JobKey = "build" | "review" | "chat" | "decisions" | "hard" | "routine";

export type JobOption = {
  engine: string;
  model: string | null;
  label: string;
  metered: boolean;
  needs_key?: boolean;
};

export type Job = {
  job: JobKey;
  label: string;
  more: boolean;
  choice: null | { engine: string; model: string | null };
  resolved: null | { engine: string; model: string | null; label: string; why: string };
  metered: boolean;
  options: JobOption[];
};

export type Budget = {
  period: "month" | "day";
  spent_usd: number;
  cap_usd: number;
  state: "ok" | "near" | "spent";
  resets_at: string;
  pill: Pill;
  monthly_cap_usd?: number;
};

export type TryRun = {
  run_id: string;
  status: string;
  pr_url: string | null;
  pr_number: number | null;
  pill: Pill;
};

export type SetupView = {
  project_id: string;
  ready: boolean;
  next: "repository" | "model" | "try" | null;
  verdict: { label: string; tone: Tone; tip: string };
  problems: number;
  secrets: { backend: string; needs_consent: boolean };
  repository: { connection: RepoConnection | null; repos: RepoEntry[] };
  models: ModelEntry[];
  detected: Detected[];
  catalogue: CatalogueEntry[];
  jobs: Job[];
  budget: Budget;
  try: { workflow_key: string; last_run: TryRun | null; done?: boolean };
  /** A background probe (signed-in CLIs, the secret store) is on its way: ask again soon. */
  detecting?: boolean;
};

/** What a refused connect or check says: one pill's worth. */
export type SetupRefusal = {
  state: string;
  headline: string;
  fix: string;
  jobs?: string[];
  resend_with?: Record<string, unknown>;
};

/** POST /v1/setup/repository/token. */
export type TokenFound = {
  /** Replace token: the bindings re-checked with the new token. */
  rechecked?: string[];
  account_login: string | null;
  expires_at: string | null;
  connection_id: string;
  repos: Array<{ full_name: string; private: boolean; default_branch: string | null;
                 owner_type: string }>;
};

/** Rows on the board. */
export type RowKey = "repository" | "models" | "jobs" | "budget" | "try";
