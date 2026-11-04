/**
 * Govern from the browser: one read per tab (GET /v1/govern, /spend, /envelopes), polled with
 * its ETag, and every write through ONE route (`/api/govern`, op-named) that sends exactly the
 * request the old Govern components sent — the same path, the same body — plus the two
 * operator-decided tightenings (the signed-in actor on Pace, FinOps and token-guide writes) and
 * an `If-Match` with the version the screen was showing.
 *
 * Shapes are the API's own (snake_case), typed here once.
 */

import { apiWrite } from "./api-write";

export type Enforced = { word: string; effect_key: string };
export type Evidence = { word: string; count_7d: number; log: { kind: string } } | null;
export type Control = {
  key: string;
  name: string;
  section: "spend" | "agents";
  value_effective: number | null;
  value_stored: unknown;
  default: unknown;
  source: "default" | "set" | "pace";
  unit: "usd_micros" | "usd_micros_per_min" | "count" | "switch" | "intensity";
  bounds: { min: number | null; max: number | null; step: number } | null;
  editable_by: "self" | "pace" | "none";
  write: { method: string; path: string } | null;
  enforced: Enforced;
  evidence: Evidence;
  advisory: boolean;
  note: { kind: "floor"; stored: number; floor: number } | { kind: "reads_as"; stored: number; reads_as: number } | { kind: "default" } | null;
  /** What the engine runs right now when something limits it (review lanes under memory pressure). */
  running_now?: { value: number; reason: "memory" } | null;
  rule?: { kind: "floor"; floor: number; fallback: number } | { kind: "zero_reads_as"; value: number } | null;
  version: string;
};
export type Period = {
  period: string; cap_usd_micros: number; actual_usd_micros: number; reserved_usd_micros: number;
  committed_usd_micros: number; utilization_ratio: number; budget_state: string; resets_at: string;
  resets_at_label: string; state_label: string;
  /** Where the period lands at today's burn (BOSSAPP-807). A projection, not money. */
  forecast_usd_micros?: number;
  /** Money spent in the period — the one "spent" (the ledger); the cap also counts `held`. */
  spent_usd_micros?: number;
  held_usd_micros?: number;
};
/** One unresolved historical call. Its old estimate is not a settled charge or a total. */
export type LegacyFinancialHold = {
  reservation_id: string; lease_id: string; invocation_id: string; receipt_id: string;
  original_reservation_status: "expired" | "released"; original_estimate_usd_micros: number;
  decision_id: string | null; message: string;
};
/** One unresolved historical provider attempt; its estimate is not a charge or a total. */
export type LegacyProviderHold = {
  reservation_id: string; changeset_id: string; operation_id: string; resource_receipt_id: string; receipt_id: string;
  original_reservation_status: "expired" | "released"; original_estimate_usd_micros: number;
  decision_id: string | null; message: string;
};
export type Meter = {
  period: string; state: string; state_label: string; cap_usd_micros: number; actual_usd_micros: number;
  reserved_usd_micros: number; committed_usd_micros: number; available_usd_micros: number;
  utilization_ratio: number; active_reservations_count: number; burn_rate_usd_micros_per_hour: number;
  burn_window_hours: number; forecasted_24h_usd_micros: number; time_to_cap_seconds: number | null;
  resets_at: string; resets_at_label: string; included_in_subscription_usd_micros: number;
  soft_cap_pct: number; auto_lite_mode_pct: number; freeze_provisioning_pct: number; freeze_tier2_pct: number;
  tier2_frozen: boolean; top_cost_drivers: { skill_or_model_id: string; amount_usd_micros: number }[];
  periods: Period[];
  legacy_financial_hold?: LegacyFinancialHold | null;
  legacy_provider_hold?: LegacyProviderHold | null;
  /** Money spent this period, one definition with get_costs (included_spend.ledger_spend). */
  ledger?: Ledger;
};
/** under_cap (settled, what caps count) + in_flight (running builds, under the caps, not settled
 *  yet) + not_under_cap (builds from before builds were metered) = money spent; included is
 *  subscription calls at list price — an estimate, never money. */
export type Ledger = {
  money_usd_micros: number; under_cap_usd_micros: number; in_flight_usd_micros?: number;
  not_under_cap_usd_micros: number;
  included_usd_micros: number; metered_calls: number; included_calls: number;
};
export type TokenPolicy = {
  stage: string; max_total_tokens_per_call: number | null; max_tokens_per_day: number | null;
  max_cost_usd_micros_per_day: number | null; alert_at_pct: number; source: string;
  updated_at?: string | null; enabled?: boolean;
};
export type GovernView = {
  project_id: string;
  meter: Meter;
  controls: Control[];
  thresholds: { key: string; label: string; pct: number; passed: boolean }[];
  token_guides: { advisory: boolean; enforced: Enforced; default: TokenPolicy | null; per_step: TokenPolicy[];
                  schema_ready: boolean };
  pace: { value: number | null; envelope: Envelope | null };
  versions: { budget: string };
  /** BOSSAPP-807: what happens when a limit is reached, as the engine reads it. */
  at_limit?: AtLimitView;
};
export type AtLimit = "pause" | "cheaper" | "stop";
export type AtLimitView = { value: AtLimit; choices: AtLimit[]; default: AtLimit; source: "set" | "default";
                            version: string; changed_by: string | null };
export type ProjectSpendRow = { id: string; name: string; money_usd_micros: number; included_usd_micros: number; calls: number };
export type ProjectLimitRow = { project_id: string; name: string; limit_usd_micros: number | null;
                                envelope_id: string | null; used_usd_micros: number };
export type ProjectLimitsView = { project_id: string; projects: ProjectLimitRow[] };
export type Envelope = {
  operational_intensity: number; daily_cap_usd_micros: number; monthly_cap_usd_micros: number;
  burst_cap_usd_micros_per_min: number; max_concurrent_jobs: number; max_workers_total: number;
};
export type DailySpend = { day: string; money_usd_micros: number; included_usd_micros: number };
export type CapPreview = {
  period: "day" | "month"; cap_usd_micros: number; days_considered: number; days_with_spend: number;
  days_stopped: number; stopped_days: string[]; not_enough_history: boolean; daily: DailySpend[];
};
export type SpendView = {
  window: string; money_usd_micros: number; included_usd_micros: number; daily: DailySpend[];
  daily_cap_usd_micros: number;
  scorecard: Record<string, unknown> & {
    calls?: number; ok_calls?: number; failed_calls?: number; cache_ratio?: number | null;
    grades?: Record<string, string>; by_stage?: Record<string, unknown>[];
  };
  savings: { kind: string; severity?: string; finding?: string; action?: string; save_usd_micros?: number;
             est_save_usd_micros?: number; message?: string; detail?: string }[];
  top_drivers: { id: string; amount_usd_micros: number }[];
  by_agent?: ModelSpendRow[];
  by_model?: ModelSpendRow[];
  spent_vs_cap: Meter;
  /** BOSSAPP-807: across every project the viewer may read. */
  by_project?: ProjectSpendRow[];
  by_model_all?: ModelSpendRow[];
  /** This project's money by model, adding up to `money_usd_micros` (`other`: no model call). */
  by_model_spent?: ModelSpendRow[];
  metered_calls?: number;
  included_calls?: number;
};
export type ModelSpendRow = { id: string; calls: number; money_usd_micros: number; included_usd_micros: number };
export type Allocation = {
  id: string; parent_allocation_id: string | null; scope_type: string; scope_id: string;
  amount_usd_micros: number; available_usd_micros: number; reserved_usd_micros: number;
  actual_usd_micros: number; period_start: string; period_end: string;
};
export type EnvelopesView = {
  allocations: Allocation[];
  finops: { state: "not_loaded" | "no_events" | "recorded"; summary: {
    days?: number; spend_by_provider?: { provider: string; cost_usd_micros: number; event_count: number }[];
    anomalies_open_count?: number; } | null };
  reconciliations: { id: string; commitment_id: string; provider: string; committed_usd_micros: number;
                     actual_usd_micros: number; utilization: number; verdict: string; decision_id: string | null }[];
  initiative_rule: { tier_min: number; amount_usd_micros: number };
};

export type GovernRead<T> =
  | { ok: true; data: T; etag: string | null; notModified?: false }
  | { ok: true; notModified: true; etag: string | null; data?: undefined }
  | { ok: false; status: number; error: string };

export async function readGovern<T>(projectId: string,
                                    view: "limits" | "spend" | "envelopes" | "daily" | "project_limits",
                                    extra: Record<string, string> = {},
                                    opts: { etag?: string | null; signal?: AbortSignal } = {}): Promise<GovernRead<T>> {
  const q = new URLSearchParams({ project_id: projectId, view, ...extra });
  try {
    const r = await fetch(`/api/govern?${q.toString()}`, {
      cache: "no-store", signal: opts.signal,
      headers: opts.etag ? { "If-None-Match": opts.etag } : undefined,
    });
    const etag = r.headers.get("etag");
    if (r.status === 304) return { ok: true, notModified: true, etag };
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, status: r.status, error: String((body as { error?: string }).error ?? `BOSS answered ${r.status}`) };
    return { ok: true, data: body as T, etag };
  } catch (e) {
    if ((e as Error)?.name === "AbortError") return { ok: false, status: 0, error: "aborted" };
    return { ok: false, status: 0, error: String((e as Error)?.message ?? e) };
  }
}

export type WriteResult =
  | { ok: true; data: Record<string, unknown> }
  | { ok: false; status: number; error: string; changedBy?: string | null };

/** One Govern write. `ifMatch` is the version the screen showed; a stale one comes back 412. */
export async function governWrite(op: string, body: Record<string, unknown>, ifMatch?: string | null): Promise<WriteResult> {
  try {
    const r = await apiWrite("/api/govern", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(ifMatch ? { "If-Match": ifMatch } : {}) },
      body: JSON.stringify({ op, ...body }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const refusal = (data as { refusal?: { changed_by?: string | null } }).refusal;
      return { ok: false, status: r.status, error: String((data as { error?: string }).error ?? `BOSS answered ${r.status}`),
               changedBy: refusal?.changed_by ?? null };
    }
    return { ok: true, data: data as Record<string, unknown> };
  } catch (e) {
    return { ok: false, status: 0, error: String((e as Error)?.message ?? e) };
  }
}

/** Money as the board says it: `$3.12`, `$200`, `$150.01`, `$5.25/min`. Cents are dropped
 *  only when they are zero — `$150.01` never reads as `$150` (Setup says the cents too). */
export function money(micros: number | null | undefined, opts: { perMin?: boolean } = {}): string {
  const cents = Math.round(Number(micros ?? 0) / 10_000);
  const usd = cents / 100;
  const text = Number(micros ?? 0) > 0 && cents === 0 ? "<$0.01"
    : usd >= 100 && cents % 100 === 0 ? `$${usd.toLocaleString("en-US")}`
    : `$${usd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return opts.perMin ? `${text}/min` : text;
}

/**
 * A typed number, checked the way the board says it (every Govern field: forms are
 * `noValidate`, so the browser can never swallow a Save — the reason is shown inline instead).
 */
export function readNumber(text: string, rule: { min?: number | null; max?: number | null; whole?: boolean;
                                                  cents?: boolean; money?: boolean }): { value: number | null; error: string | null } {
  const raw = text.trim();
  if (raw === "") return { value: null, error: "Enter a number" };
  const n = Number(raw);
  const fmt = (x: number) => (rule.money ? `$${x.toLocaleString("en-US")}` : x.toLocaleString("en-US"));
  const range = rule.min != null && rule.max != null ? `${fmt(rule.min)}–${fmt(rule.max)}`
    : rule.min != null ? `at least ${fmt(rule.min)}` : rule.max != null ? `at most ${fmt(rule.max)}` : "";
  if (!Number.isFinite(n)) return { value: null, error: "Enter a number" };
  if (rule.whole && !Number.isInteger(n)) return { value: null, error: `Whole numbers${range ? `, ${range}` : ""}` };
  if (rule.cents && Math.abs(n * 100 - Math.round(n * 100)) > 1e-6) return { value: null, error: "Cents at most" };
  if ((rule.min != null && n < rule.min) || (rule.max != null && n > rule.max)) return { value: null, error: range.replace(/^./, (c) => c.toUpperCase()) };
  return { value: n, error: null };
}

/** What a stored value will run as, by the engine's rule (served with the control). */
export function effectiveOf(control: Pick<Control, "rule">, stored: number | null): number | null {
  if (stored === null || Number.isNaN(stored)) return null;
  const rule = control.rule;
  if (rule?.kind === "floor") return Math.max(stored || rule.fallback, rule.floor);
  if (rule?.kind === "zero_reads_as") return Math.max(0, stored || rule.value);
  return stored;
}

/** The 403 tooltip names the permission a write needs. */
export function rightsFor(op: string): string {
  return op === "setting" ? "Needs provider rights" : "Needs budget rights";
}

const MODEL_FAMILY = /^(claude|gpt|gemini|o\d|llama|mistral|mixtral|grok|deepseek|qwen|command)/i;
const WORD: Record<string, string> = { claude: "Claude", gpt: "GPT", gemini: "Gemini", llama: "Llama",
  opus: "Opus", sonnet: "Sonnet", haiku: "Haiku", fable: "Fable", mini: "mini", turbo: "Turbo", pro: "Pro",
  flash: "Flash", mistral: "Mistral", deepseek: "DeepSeek", qwen: "Qwen", grok: "Grok" };

const PROVIDER: Record<string, string> = {
  openai: "OpenAI", anthropic: "Anthropic", claude: "Anthropic", google: "Google", gemini: "Google Gemini",
  vertex: "Vertex AI", azure: "Azure OpenAI", azure_openai: "Azure OpenAI", bedrock: "AWS Bedrock",
  mistral: "Mistral", groq: "Groq", deepseek: "DeepSeek", openrouter: "OpenRouter", ollama: "Ollama",
  together: "Together AI", fireworks: "Fireworks", xai: "xAI", cohere: "Cohere", custom: "Custom model",
  claude_code: "Claude Code", codex: "Codex", claude_agent_sdk: "Claude Agent SDK",
};

/** A model id as people say it: `claude-sonnet-4-5-20250929` → `Claude Sonnet 4.5`,
 *  `gpt-4o` → `GPT-4o`; a Setup source id (`setup-openai`, `setup-openai-2`) → `OpenAI`
 *  (`OpenAI 2`). Anything else reads as words (`repo_scan` → `Repo scan`). */
export function modelName(id: string): string {
  let raw = String(id ?? "").trim();
  if (!raw) return "Unknown";
  if (raw === "other") return "Other spend";
  // A context-size suffix (`claude-opus-4-8[1m]`) is said as `(1M)`, not mangled into the name.
  const ctx = raw.match(/\[(\w+)\]$/);
  if (ctx) {
    raw = raw.slice(0, -ctx[0].length);
    return `${modelName(raw)} (${ctx[1].toUpperCase()})`;
  }
  const setup = raw.match(/^setup[-_:]([a-z][a-z0-9_]*?)(?:[-_](\d+))?$/i);
  if (setup) {
    const name = PROVIDER[setup[1].toLowerCase()] ?? humanWords(setup[1]);
    return setup[2] ? `${name} ${setup[2]}` : name;
  }
  if (PROVIDER[raw.toLowerCase()]) return PROVIDER[raw.toLowerCase()];
  if (!MODEL_FAMILY.test(raw)) return humanWords(raw);
  const parts = raw.replace(/[-@](\d{8}|latest)$/i, "").split(/[-_/]/).filter(Boolean);
  if (/^gpt$/i.test(parts[0]) && parts.length > 1) return `GPT-${parts.slice(1).join("-")}`;
  const out: string[] = [];
  for (const part of parts) {
    const prev = out[out.length - 1];
    if (/^\d+$/.test(part) && prev && /^\d+(\.\d+)*$/.test(prev)) out[out.length - 1] = `${prev}.${part}`;
    else out.push(WORD[part.toLowerCase()] ?? part);
  }
  return out.join(" ");
}

function humanWords(raw: string): string {
  const words = raw.replace(/[_:]+/g, " ").replace(/\s+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A tooltip within the copy budget: at most 14 words. */
export function tip14(text: string | null | undefined): string | undefined {
  const words = String(text ?? "").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return undefined;
  return words.length <= 14 ? words.join(" ") : `${words.slice(0, 13).join(" ")}…`;
}
