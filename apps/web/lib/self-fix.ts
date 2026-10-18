import { consoleHref } from "../app/url-intent";
import { apiWrite } from "./api-write";

/**
 * The console side of BOSS fixes itself (docs/product/BOSS_FIXES_ITSELF.md §10, §11.4, §12).
 * Reads go to GET /api/self-fix?view=…, writes to POST /api/self-fix {op} (lib/self-fix-ops.ts).
 * Shapes are the API's own (snake_case), typed here once.
 */
export type SelfFixRead<T> =
  | { ok: true; data: T; etag: string | null; notModified?: false }
  | { ok: true; notModified: true; etag: string | null }
  | { ok: false; status: number; error: string };

export async function readSelfFix<T>(view: string, params: Record<string, string>,
                                     opts: { etag?: string | null; signal?: AbortSignal } = {}): Promise<SelfFixRead<T>> {
  const q = new URLSearchParams({ view, ...params });
  try {
    const r = await fetch(`/api/self-fix?${q.toString()}`, {
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
export function polledSelfFix<T>(view: string, params: Record<string, string>) {
  return async (etag: string | null, signal: AbortSignal) => {
    const got = await readSelfFix<T>(view, params, { etag, signal });
    if (!got.ok) return { ok: false as const, error: got.error };
    if (got.notModified) return { ok: true as const, notModified: true as const, etag: got.etag };
    return { ok: true as const, data: got.data, etag: got.etag };
  };
}

export type SelfFixWrite = { ok: true; data: Record<string, unknown> } | { ok: false; status: number; error: string };

export async function selfFixWrite(op: string, body: Record<string, unknown>): Promise<SelfFixWrite> {
  try {
    // One press, one effect: a double click joins the first request (lib/api-write.ts).
    const r = await apiWrite("/api/self-fix", {
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

export function selfFixHref(view: string, params: Record<string, string>): string {
  return `/api/self-fix?${new URLSearchParams({ view, ...params }).toString()}`;
}

// ------------------------------------------------------------------------------------- types

export type Chip = "Building" | "Needs you" | "Installed" | "Verified" | "Rolled back" | "Refused"
  | "Sent upstream" | "In release";
export const CHIPS: Chip[] = ["Building", "Needs you", "Installed", "Verified", "Rolled back", "Refused",
  "Sent upstream", "In release"];
/** Each chip's tone and what it means, said once for the list, the lineage and the strip. */
export const CHIP_TONE: Record<Chip, { tone: string; tip: string }> = {
  Building: { tone: "blue", tip: "Mender is writing and checking the fix" },
  "Needs you": { tone: "amber", tip: "Waiting for your approval to install" },
  Installed: { tone: "accent", tip: "On this machine; the stuck step is being re-run" },
  Verified: { tone: "green", tip: "The stuck step passed with the fix installed" },
  "Rolled back": { tone: "red", tip: "It did not hold; the previous build is back" },
  Refused: { tone: "muted", tip: "BOSS did not change itself; a person decides" },
  "Sent upstream": { tone: "blue", tip: "Sent to the BOSS team" },
  "In release": { tone: "green", tip: "An official release contains it" },
};
export function chipOf(chip: string | null | undefined): Chip {
  return (CHIPS as string[]).includes(String(chip)) ? (chip as Chip) : "Building";
}

export type DiffStat = { files?: Array<{ path: string; added?: number; removed?: number } | string>;
  added?: number; removed?: number };
export type SelfFixItem = {
  id: string; status: string; chip: string; stage: string | null; area: string; signature: string;
  class_signature: string; run_id: string | null; step_id: string | null; confidence: string;
  tier: number | null; flags: string[]; refusal: string | null; refusal_detail: string | null;
  diff_stat: DiffStat; cost_micros: number; budget_usd_micros: number; decision_id: string | null;
  upstream_state: "local_only" | "previewed" | "sent" | "merged" | "closed" | "duplicate_of";
  upstream_url: string | null; stack_state: string | null;
  pr: { title?: string; labels?: string[]; branch?: string; body?: string; trailers?: Record<string, string> };
  created_at: string; updated_at: string; line: string;
};
export type ListView = { self_fixes: SelfFixItem[]; count: number };
export type LineageStep = { key: string; label: string; state: "done" | "now" | "next" | "failed" | "skipped";
  at: string | null; detail: string; href?: string };
export type Card = {
  self_fix_id: string; title: string; tier: number; flags: string[]; what_broke: string; why: string;
  rejected: Array<{ hypothesis?: string; why?: string }>; confidence: { level: string; reasons: string[] };
  change: { files: string[]; added: number; removed: number; host_effect: string[] };
  tests: { new: string[]; red_on_base: boolean; green_with_fix: boolean; affected: number; p0: number; none_weakened: boolean };
  review: Record<string, { verdict: string; engine?: string; findings?: string[] }>;
  security_on_another_engine: boolean; review_note: string; cost_usd: number; minutes: number; engine: string;
  on_approve: string; undo: string; people: string; command: string;
};
export type OneView = {
  self_fix: SelfFixItem & { evidence?: Record<string, unknown>; diagnosis?: Record<string, unknown>;
    red_green?: Record<string, unknown>; gates?: Record<string, { ok?: boolean; detail?: unknown }>;
    review?: Record<string, unknown>; verification?: Record<string, unknown> };
  events: Array<{ event_type: string; actor: string; data: { sentence?: string } & Record<string, unknown>; created_at: string }>;
  tickets: Array<{ id: string; kind: string; status: string; phases: Array<{ phase: string; ok?: boolean; detail?: string; at?: string }>; created_at: string }>;
  waiters: Array<{ run_id: string; step_id: string }>;
  line: string; card?: Card; lineage: LineageStep[];
};
export type DiffView = { diff: string; diff_stat: DiffStat; tests: string[];
  red: { exit_code?: number; summary?: string }; green: { exit_code?: number; summary?: string };
  gates: Record<string, { ok?: boolean; detail?: unknown }>;
  review: { seats?: Record<string, { verdict?: string; engine?: string; findings?: unknown }> } };
export type Settings = {
  self_fix_min_confidence: "low" | "medium" | "high"; self_fix_budget_cents: number; self_fix_month_cents: number;
  self_fix_max_minutes: number; self_fix_attempts_per_signature: number; self_fix_max_diff_lines: number;
  self_fix_health_minutes: number;
};
export type SettingsView = {
  switch: { on: boolean; stored?: boolean; set_by?: string | null; at?: string | null; why?: string };
  settings: Settings; auto_install: "off"; auto_install_why: string; can_install: boolean; why_not: string;
  stack: Array<{ position: number; self_fix_id: string; commit_sha: string; status: string; title: string | null; class_signature: string }>;
  env_off: boolean; month_spent_usd: number; installer: { last_seen_at: string | null; watching: boolean };
};
export type Finding = { where: "diff" | "title" | "body" | "evidence" | "trailers"; kind: string; detail: string };
export type Send = {
  send_id: string; status: "previewed" | "sent" | "blocked" | "exported" | "failed";
  channel: "github_pr" | "github_comment" | "collector" | "patch_export"; channel_why: string;
  target: { upstream: string; fork: string | null; pr_url: string | null };
  title: string; labels: string[]; diff_stat: DiffStat; diff: string; body: string; evidence: string;
  trailers: Record<string, string>; check: { ok: boolean; findings: Finding[] };
  scrubbed: { body: number; evidence: number };
  dedupe: { state: "none" | "merged" | "open_pr" | "closed_pr" | "open_issue" | "skipped"; checked_at: string | null;
            match: { url: string; number: number; title: string } | null; why: string };
  sha256: string; checked: boolean; created_at: string; result: { url?: string; error?: string } | null;
};
export type UpstreamView = { send: Send | null; telemetry_off: string | null };
export type Counts = { problems: number; by_boss: number; by_person: number; by_outside_agent: number; open: number };
export type AutonomyView = {
  window: string; totals: Counts & { rate: number | null; hands_off_rate: number | null };
  boss_side: Counts & { rate: number | null };
  trend: Array<Counts & { week: string; rate: number | null }>;
  runs: Array<Counts & { run_id: string; name: string; rate: number | null; sentence: string; ended_at: string | null }>;
};
export type RunAutonomyView = { run_id: string; counts: Counts; rate: number | null; sentence: string;
  problems: Array<{ step_id: string; signature: string; resolved_by: "boss" | "person" | "outside_agent" | "open";
                    how: string; self_fix_id: string | null; started_at: string | null; ended_at: string | null }> };
export type Recommendation = { id: string; signature: string; status: string; title: string; symptom: string | null;
  root_cause: string | null; proposed_change: string | null; files: string[]; occurrences: number;
  self_fix_id: string | null; class_signature: string | null; attempts: Array<{ self_fix_id?: string; outcome?: string; why?: string }>;
  created_at: string; updated_at: string };
export type RecommendationsView = { recommendations: Recommendation[]; count: number };
/** The step's own view of its self-fix (engine self_fix.step_view), as the step output carries it. */
export type StepSelfFix = {
  id: string; status: string; chip: string; label: string; stage: string | null;
  stages: Array<{ stage: string; state: "done" | "now" | "next" }>; line: string; needs_you: boolean;
  decision_id: string | null; waiting_on?: boolean;
  facts?: { files?: string[]; tests_added?: number; cost_usd?: number; budget_usd?: number; minutes?: number };
};

// ------------------------------------------------------------------------------------- words

/** The parked step's eight stages (§10.1), each a short word and its tooltip. */
export const STAGE_WORDS: Record<string, { word: string; tip: string }> = {
  found: { word: "Found", tip: "BOSS located the bug in its own code" },
  reproduced: { word: "Reproduced", tip: "BOSS wrote a test that fails because of this bug" },
  fixing: { word: "Fixing", tip: "The smallest change that makes the test pass" },
  checks: { word: "Checks", tip: "BOSS's own tests, and no test weakened" },
  review: { word: "Review", tip: "Code and security reviewers read the change" },
  you: { word: "You", tip: "Waiting for your approval" },
  install: { word: "Install", tip: "BOSS rebuilds itself with the fix and checks its health" },
  resume: { word: "Resume", tip: "The stuck step runs again; passing is the proof" },
};

export function usd(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return `$${(Number.isFinite(v) ? v : 0).toFixed(2)}`;
}
export function micros(n: number | null | undefined): string { return usd(Number(n ?? 0) / 1e6); }
export function pct(rate: number | null | undefined): string {
  return rate === null || rate === undefined || !Number.isFinite(rate) ? "—" : `${Math.round(rate * 100)}%`;
}
export function fileCount(stat: DiffStat | null | undefined): number { return (stat?.files ?? []).length; }
export function filePath(f: { path: string } | string): string { return typeof f === "string" ? f : f.path; }
export function shortSha(sha: string | null | undefined): string { return (sha ?? "").slice(0, 12); }

/** Where one self-fix opens in the console (keeping who you are). */
export function selfFixAreaHref(id?: string | null): string {
  return consoleHref(typeof window === "undefined" ? "" : window.location.search,
                     { area: "self_fixes", sfx: id || null });
}
