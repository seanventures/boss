import { apiWrite } from "./api-write";

/**
 * The console side of Improve BOSS, run scorecards, experiments and the person's feedback.
 * Reads go to GET /api/improve?view=…, writes to POST /api/improve {op} (lib/improve-ops.ts).
 */
export type ImproveRead<T> =
  | { ok: true; data: T; etag: string | null; notModified?: false }
  | { ok: true; notModified: true; etag: string | null }
  | { ok: false; status: number; error: string };

export async function readImprove<T>(view: string, params: Record<string, string>,
                                     opts: { etag?: string | null; signal?: AbortSignal } = {}): Promise<ImproveRead<T>> {
  const q = new URLSearchParams({ view, ...params });
  try {
    const r = await fetch(`/api/improve?${q.toString()}`, {
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

/** For usePolledRead: the same read, in its shape. */
export function polledImprove<T>(view: string, params: Record<string, string>) {
  return async (etag: string | null, signal: AbortSignal) => {
    const got = await readImprove<T>(view, params, { etag, signal });
    if (!got.ok) return { ok: false as const, error: got.error };
    if (got.notModified) return { ok: true as const, notModified: true as const, etag: got.etag };
    return { ok: true as const, data: got.data, etag: got.etag };
  };
}

export type ImproveWrite = { ok: true; data: Record<string, unknown> } | { ok: false; status: number; error: string };

export async function improveWrite(op: string, body: Record<string, unknown>): Promise<ImproveWrite> {
  try {
    // One press, one effect: a double click joins the first request (lib/api-write.ts).
    const r = await apiWrite("/api/improve", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op, ...body }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, status: r.status, error: String((data as { error?: string }).error ?? `BOSS answered ${r.status}`) };
    return { ok: true, data: data as Record<string, unknown> };
  } catch (e) {
    return { ok: false, status: 0, error: String((e as Error)?.message ?? e) };
  }
}

export function improveHref(view: string, params: Record<string, string>): string {
  return `/api/improve?${new URLSearchParams({ view, ...params }).toString()}`;
}

// ------------------------------------------------------------------------------------- types

export type Switch = { on: boolean; by: string | null; by_name?: string | null; at: string | null; label: string; default: boolean; asked: boolean };
export type Weight = { key: string; label: string; value: number; default: number };
export type SendRow = { id: string; day: string; kind: string; status: string; bytes_raw: number; bytes_gz: number;
  batch_days: string[]; aggregated: Record<string, unknown>; http_status: number | null; error: string | null;
  cut_at: string; sent_at: string | null };
export type SharingView = {
  switches: { improve: Switch; heartbeat: Switch; collector: Switch };
  env_disabled: string | null; blocked?: string | null; install_id: string; url: string;
  url_source?: "env" | "govern" | "none"; retired_waiting?: number; mask_words?: string[]; next_due_at: string | null;
  last_attempt_at: string | null; sends: SendRow[]; unsent_batches: number;
  score: { version: string; weights: Weight[] }; heartbeat_fields: string[];
};
export type PreviewView = {
  pending: null | { id: string; day: string; cut_at: string; bytes_raw: number; bytes_gz: number;
    aggregated: Record<string, unknown>; batch_days: string[]; sha256: string; json: Record<string, unknown> };
  why_not: string | null; blocked?: string | null; next_due_at: string | null;
};
export type ScoreRow = { key: string; label?: string; n: number; score: number | null; sd: number | null; done: number | null;
  merged: number | null; cost_usd: number | null; turns: number | null; steers: number | null; rounds: number | null };
export type CostingRow = { unit_kind: string; unit_id: string; why: string; title: string; score: number | null;
  steers: number; rounds: number; cost_usd: number; role_key: string | null; role_name?: string | null; state: string;
  error_class: string | null; ended_at: string | null; href: string };
export type ScorecardsView = {
  group: string; win: string; kind: string; score_version: string; weights: Weight[];
  total: { n: number; score: number | null; cost_usd: number | null; done: number | null; merged: number | null; backfilled: number };
  rows: ScoreRow[]; costing: CostingRow[];
};
export type Arm = { n: number; mean: number | null; sd: number | null; cost_usd: number; gate_fail: number | null; assigned: number };
export type Experiment = {
  id: string; name: string; target_kind: string; target_key: string; knob: string; knob_label: string;
  arm_a: unknown; arm_b: unknown; arm_a_label?: string; arm_b_label?: string; split_b: number; target_units: number; arm_cost_cap_usd: number;
  status: string; stop_reason: string | null; capped_arms: string[]; promoted_arm: string | null;
  arms: { a: Arm; b: Arm };
  promote?: Record<"a" | "b", { setting: string; label: string; from: unknown; to: unknown; enough: boolean; why_not: string }> | null;
  compare: { verdict: string; diff: number | null; low: number | null; high: number | null; label: string; why: string };
};
export type ExperimentsView = { items: Experiment[];
  targets?: { role: { key: string; name: string }[]; workflow: { key: string; name: string }[] };
  knobs: { key: string; label: string; kind: string; choices: string[] | null; min: number | null; max: number | null }[] };
export type PromptsView = { ask_open: boolean; asks_left_today: number; checkin_due: boolean;
  mine: Record<string, { rating: string | null; chips: string[] }>;
  chips: Record<string, string[]>; ratings: Record<string, string[]> };
export type KeyN = { key: string; n: number };
export type CollectorView = {
  proof: string; active: { daily: number; weekly: number; monthly: number }; total_installs: number;
  kept_30d_pct: number | null; new_per_week: { week: string; n: number }[];
  cohorts: { week: string; size: number; pct: number[] }[];
  versions: KeyN[]; editions: KeyN[]; os: KeyN[]; arch: KeyN[]; methods: KeyN[]; opted_in: number;
  scale: Record<string, { at_least: number; dist: KeyN[] }>;
  ship: { rated: number; yes: number; with_changes: number; no: number; rate: number | null };
  chips: KeyN[]; funnel: KeyN[]; checkin: KeyN[];
  by: Record<string, { key: string; name?: string; n: number; score: number | null; merged: number | null }[]>;
  held_back?: number;
  errors: KeyN[]; installer: null;
};

/** What the console calls a harness (never its key). */
export const HARNESS_NAMES: Record<string, string> = {
  claude_code: "Claude Code", codex: "Codex", native: "BOSS native", openai: "OpenAI API", azure: "Azure OpenAI",
  openrouter: "OpenRouter", groq: "Groq", deepseek: "DeepSeek", ollama: "Ollama", vllm: "vLLM", grok: "Grok",
  cursor: "Cursor", gemini: "Gemini", echo: "Echo",
};

/** A time and date in the reader's locale: "Mon 5 Oct, 18:20". */
export function stamp(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** Words for the person: chips and ratings as the console says them (≤ 3 words each). */
export const CHIP_WORDS: Record<string, string> = {
  wrong_scope: "Wrong scope", poor_quality: "Poor quality", broke_something: "Broke something",
  too_slow: "Too slow", too_expensive: "Too expensive", misread_brief: "Misread brief",
  shallow: "Shallow", wrong_market: "Wrong market", great_sources: "Great sources",
  changed_my_mind: "Changed my mind", missed_requirements: "Missed requirements",
  over_scoped: "Over-scoped", good_debate: "Good debate",
};
export const RATING_WORDS: Record<string, string> = {
  yes: "Yes", with_changes: "With changes", no: "No", useful: "Useful", not_useful: "Not useful",
  right: "Got it right", wrong: "Missed", up: "Helpful", down: "Not helpful",
  working: "Working", not_working: "Not working",
};
export const QUESTION: Record<string, string> = {
  build: "Would you ship this?", research: "Was this useful?", panel: "Did the panel get it right?",
  answer: "Helpful?", deploy: "Working as expected?",
};

export function ago(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function kb(bytes: number | null | undefined): string {
  const n = Number(bytes ?? 0);
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
}

// ------------------------------------------------------------------------------------- shape
// A read is used only when it has the shape this screen draws: an old API, a proxy's error page
// or another screen's answer must make the control quietly absent, never crash the page.
const isObj = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);
export function isPrompts(x: unknown): x is PromptsView {
  return isObj(x) && isObj(x.ratings) && isObj(x.chips) && isObj(x.mine) && typeof x.ask_open === "boolean";
}
export function isSharing(x: unknown): x is SharingView {
  return isObj(x) && isObj(x.switches) && isObj((x.switches as Record<string, unknown>).improve)
    && isObj((x.switches as Record<string, unknown>).heartbeat) && Array.isArray(x.sends) && isObj(x.score);
}
