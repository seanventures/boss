/**
 * Repo and models: the frozen contract with GET /v1/setup (apps/api/app/schemas_setup.py).
 *
 * The server decides every pill — label, tone, tooltip, action — so the console only renders.
 * Nothing in apps/web computes a tone from a state (tests/setup-renders-server-pills.test.tsx).
 */
import type { SetupDetectedFound } from "@boss/api-client";

export type Tone = "green" | "amber" | "red" | "grey" | "busy";
/** `sign_in`: a subscription whose login failed — it opens Sign in, never Replace (BOSSAPP-769). */
export type PillAction = "connect" | "test" | "replace" | "fix_link" | "budget" | "default_model"
  | "sign_in";

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
  /** The org's connection (every project holds it) or one this project made itself. */
  scope?: "org" | "project";
  /** Claude Code only: what it runs on — "long-lived subscription token (renews …)" or
   *  "CLI login (expires in X h)". Never a secret. */
  credential?: { kind: "long_lived_token" | "cli_login"; label: string; expires_at: string | null } | null;
};

/** Subscriptions: signed_in | needs_sign_in | not_on_server (nothing is pasted any more). Local:
 *  found | not_on_server. The wire type lives in @boss/api-client (SetupDetectedFound). */
export type Found = SetupDetectedFound;

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
  /** Local › Ollama: where BOSS looks when the URL is left empty (OLLAMA_BASE_URL). */
  example_base_url?: string | null;
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
  /** Whose choice: the org's (every project) or this project's pin. Absent on an older API. */
  scope?: "org" | "project";
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

export type Toolchain = {
  name: string;
  label: string;
  installed: boolean;
  builtin: boolean;
  versions: Record<string, string>;
  /** How to turn it on, when it is off. */
  enable?: string | null;
};

/** A connection proof whose money is held until a person reviews it (GET /v1/setup held_proofs). */
export type HeldProof = {
  receipt_id: string;
  created_at: string | null;
  provider: string;
  provider_label: string;
  model: string;
  actor?: string | null;
  held_usd_micros: number;
  summary: string;
  outcome: { kind: "replied_ok" | "refused" | "unknown"; http_status?: number | null;
             provider_error_type?: string; provider_error?: string; usage_present?: boolean | null };
  /** What "Release the hold" settles it at: 0 if the provider refused, else this estimate. */
  release: { cost_usd_micros: number; basis: "refused" | "priced" | "unpriced"; succeeded: boolean };
};

export type ClaudeCredential = {
  order: "subscription_first" | "api_key_first";
  active: "subscription" | "cli_login" | "stored_key" | "env_key" | "none";
  billing?: string | null;
  subscription_disconnected?: boolean;
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
  /** Held connection proofs (Review → Release the hold). Absent on an older API. */
  held_proofs?: HeldProof[];
  jobs: Job[];
  /** Null when the server has no router tables yet (an older API): the row shows the jobs. */
  routing?: Routing | null;
  budget: Budget;
  try: { workflow_key: string; last_run: TryRun | null; done?: boolean };
  /** Build toolchains in this install's image. Absent on an older API. */
  toolchains?: Toolchain[];
  /** "Give builds a database (Postgres)" — the project setting build_services. Absent on an older API. */
  build_database?: { key: string; mode?: "auto" | "always" | "never"; enabled: boolean; source: "project" | "default"; configured: boolean;
                     permission: string } | null;
  /** "Build speed": parallel sub-agents per build (project setting build_speed). Absent on an older API. */
  build_speed?: { key: string; mode: "balanced" | "fastest"; parallelism: number; machine: number;
                  asks?: Record<string, number>; source: "project" | "default"; permission: string } | null;
  /** Setup › Repo and models › Embedding model, the org's (knowledge/embedding_slot.py). Absent on an older API. */
  embedding_model?: EmbeddingModel | null;
  /** Whose models these are (org_models.view): the org's, its model home, this project's pins. */
  models_scope?: ModelsScope | null;
  /** Which credential Claude Code runs on, and the order (claude_credential.describe). */
  claude_credential?: ClaudeCredential | null;
  /** A background probe (signed-in CLIs, the secret store) is on its way: ask again soon. */
  detecting?: boolean;
  /** Setup › GitHub accounts (BOSSAPP-808): one row per account. Absent on an older API. */
  github_accounts?: GitHubAccount[];
  /** Setup › Repositories BOSS can work on (BOSSAPP-808). Absent on an older API. */
  repositories?: SetupRepository[];
};

/** How an account reaches GitHub (engine services/setup_accounts.py). */
export type GitHubAccountKind = "personal_signin" | "org_app" | "token" | "org_token" | "install";

export type GitHubAccount = {
  id: string;
  /** This project's GitHub connection (Replace token, Add repo); null for the org's own rows. */
  connection_id: string | null;
  scope: "project" | "org" | "install";
  kind: GitHubAccountKind;
  name: string;
  login: string | null;
  /** "Personal · signed in with GitHub", "Organisation · BOSS GitHub App", … */
  how: string;
  personal: boolean | null;
  repo_count: number | null;
  expires_at: string | null;
  /** Server-decided: green Connected, amber expiring, red Broken (action says the fix). */
  pill: Pill;
  created_at?: string | null;
};

export type SetupRepository = {
  full_name: string;
  binding_id: string | null;
  /** Bound to this project (false: allowed for the org, used elsewhere or nowhere yet). */
  in_project: boolean;
  default: boolean;
  account_id: string | null;
  /** The account's name ("via paramg"). */
  via: string | null;
  projects: { project_id: string; name: string }[];
  checks: { read: CheckResult; write: CheckResult; pull_requests: CheckResult } | null;
  pill: Pill | null;
};

/** Setup › Repo and models › Models is the org's: set once, every project uses it. */
export type ModelsScope = {
  scope: "org" | "project";
  org_id: string | null;
  home_project: string | null;
  is_home: boolean;
  overrides: { key: string; label: string; value: unknown; org_value: unknown }[];
  own_sources: string[];
};

/** One provider Setup offers for the embedding model, with its form. */
export type EmbeddingProvider = {
  provider: "azure" | "openai" | "ollama" | string; label: string; needs_key: boolean;
  fields: string[];
  /** Fields under the row's "More" toggle (deployment, API version, a vector size). */
  more?: string[];
  example_model?: string; example_dimension?: number; example_base_url?: string;
};

/** The embedding model's slot: only THAT a key is held, never the key. */
export type EmbeddingModel = {
  key: string; configured: boolean; permission: string; budget_category: string;
  /** Meaning-based search is on (the last check passed); `reason` says why not in words. */
  search: boolean; reason: string;
  providers: EmbeddingProvider[];
  /** "org": every project of the org uses it. "project": an older install's own row. */
  scope?: "org" | "project"; projects?: number; place?: string;
  /** The size was set by the operator (asked of the model); otherwise learned from the check. */
  dimension_set?: boolean;
  provider?: string; label?: string; model?: string; dimension?: number; base_url?: string;
  endpoint?: string; deployment?: string; api_version?: string; has_key?: boolean; model_id?: string;
  check?: { state: "ok" | "failed" | "unchecked"; reply?: string; error?: string; checked_at?: string;
            latency_ms?: number };
  /** Summaries still to embed under this model (the background re-embed). */
  reembed_pending?: number;
};

/** What a refused connect or check says: one pill's worth. */
export type SetupRefusal = {
  state: string;
  headline: string;
  fix: string;
  /** What the provider actually said, in words ("Anthropic refused: model not found"). */
  summary?: string;
  /** A held proof's receipt: Review it to release the hold. */
  receipt_id?: string;
  reconciliation_required?: boolean;
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
export type RowKey = "github" | "repository" | "models" | "capacity" | "jobs" | "budget" | "try"
  /** Advanced: the rows folded under it (`focus=` opens the fold on that row). */
  | "advanced" | "github_app" | "repositories" | "ci" | "build_database" | "build_speed" | "toolchains" | "public_url"
  | "embedding_model";
