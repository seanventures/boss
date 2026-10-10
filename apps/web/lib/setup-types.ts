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
  /** The instant a time in the label stands for; the console shows it in local time. */
  at?: string | null;
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
  /** Whether builds can use this token (they read the org's GitHub token). */
  builds?: { ok: boolean; message?: string; next?: string } | null;
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
  /** Where a key or local model points (no userinfo): Replace keeps it. */
  base_url?: string | null;
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

/** Setup › Model routing (docs/product/MODEL_ROUTER.md). Decided by the server. */
export type Tier = "fast" | "balanced" | "frontier";
export type RouteTarget = { engine: string; model: string | null; label: string; connected?: boolean };
export type RoutingFloor = {
  match: Record<string, string | string[]>; tier: Tier; label?: string;
  describe: string; tier_label: string;
};
export type RoutingHeadroom = {
  engine: string; source_label: string; state: "plenty" | "tight" | "near_limit" | "exhausted";
  label: string; left: number | null; resets_at: string | null; basis: string;
  exhausted_models: string[]; describe: string; detail?: string;
};
export type HeadroomRule = { source: string; when: string; tiers: Tier[]; to: string };
export type Routing = {
  mode: "fixed" | "smart";
  version: number;
  tiers: { tier: Tier; label: string; criteria: string; targets: RouteTarget[] }[];
  floors: RoutingFloor[];
  floor_preset: string;
  presets: { name: string; label: string }[];
  floor_steps: { step: string; label: string }[];
  headroom: RoutingHeadroom[];
  headroom_rules: HeadroomRule[];
  headroom_states: { state: string; label: string }[];
  thresholds: { tight: number; near_limit: number };
  budget: Record<string, number>;
  budget_state: string;
  classifier: {
    backend: string; jev: "ready" | "held" | "absent"; model_fallback: boolean;
    model_below: number; criteria: Record<Tier, string>;
    backends: { value: string; label: string }[];
  };
  sources: { engine: string; label: string; kind: string; working: boolean;
             models: (string | null)[] }[];
  pins: Record<string, { engine: string; model: string | null; label: string }>;
  /** Jobs whose pin is "Always use, even when limited"; any other pin is a preference. */
  hard_pins?: string[];
  frontier_allows_local?: boolean;
  week: { routes: number; smart_routes: number; tiers: Record<Tier, number>;
          escalations: number; moved: number; saved_usd: number };
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
  verdict: { label: string; tone: Tone; tip: string; at?: string | null };
  problems: number;
  secrets: { backend: string; needs_consent: boolean };
  repository: { connection: RepoConnection | null; repos: RepoEntry[] };
  models: ModelEntry[];
  detected: Detected[];
  catalogue: CatalogueEntry[];
  jobs: Job[];
  /** Null when the server has no router tables yet (an older API): the row shows the jobs. */
  routing?: Routing | null;
  budget: Budget;
  try: { workflow_key: string; last_run: TryRun | null; done?: boolean };
  /** "Give builds a database (Postgres)" — the project setting build_services. Absent on an older API. */
  build_database?: { key: string; mode?: "auto" | "always" | "never"; enabled: boolean; source: "project" | "default"; configured: boolean;
                     permission: string } | null;
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
  /** "github_app": the App connected in GitHub access, not a pasted token. */
  source?: "github_app";
  repos: Array<{ full_name: string; private: boolean; default_branch: string | null;
                 owner_type: string }>;
};

/** Rows on the board. */
export type RowKey = "repository" | "models" | "capacity" | "jobs" | "budget" | "try";
