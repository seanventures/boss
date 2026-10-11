/**
 * Repo and models' layout decisions, as pure functions of the server's view.
 *
 * While setup is unfinished the board runs as a stepper — 1 Repository, 2 Model, 3 Try it —
 * with only the current row open; after the first pull request it is a board with no numbers.
 * The server says which step is next (`view.next`); this only turns that into row modes.
 */
import type { RowKey, SetupView } from "./setup-types";

export type RowMode = "current" | "done" | "waiting" | "collapsed" | "open";

/** First-run (stepper) mode: something on the path to the first PR is still to do. */
export function firstRun(view: SetupView): boolean {
  return view.next !== null;
}

/** The row the stepper is on, or null on the board. */
export function nextStep(view: SetupView): RowKey | null {
  if (view.next === "repository") return "repository";
  if (view.next === "model") return "models";
  if (view.next === "try") return "try";
  return null;
}

const ORDER: RowKey[] = ["repository", "models", "try"];

/** How a row draws. Board mode: everything open. Stepper: the current step open, finished
 *  steps one line, later steps waiting, and Who does what / Budget collapsed to defaults. */
export function rowMode(view: SetupView, row: RowKey): RowMode {
  const step = nextStep(view);
  if (step === null) return "open";
  if (row === "jobs" || row === "budget" || row === "capacity") return "collapsed";
  const at = ORDER.indexOf(step);
  const me = ORDER.indexOf(row);
  if (me === at) return "current";
  return me < at ? "done" : "waiting";
}

/** The step number a row carries in first-run mode (①②③), or null. */
export function stepNumber(view: SetupView, row: RowKey): number | null {
  if (!firstRun(view)) return null;
  const n = ORDER.indexOf(row);
  return n < 0 ? null : n + 1;
}

export type SetupParams = { focus: RowKey | null; add: string | null; source: string | null };

const ROWS: RowKey[] = ["github", "repository", "models", "capacity", "jobs", "budget", "try",
                        "advanced", "github_app", "repositories", "ci", "build_database", "toolchains",
                        "public_url"];

/** Rows folded under Advanced: a deep link to one opens the fold first. */
export const ADVANCED_ROWS: readonly RowKey[] = ["advanced", "github_app", "repositories", "ci", "build_database",
                                                 "toolchains", "jobs", "budget", "public_url"];

/** `?area=setup[&focus=…][&add=<provider>][&source=<id>]`, plus the legacy `step=` names. */
export function readParams(search: string): SetupParams {
  const params = new URLSearchParams(search);
  let focus = params.get("focus") as RowKey | null;
  const step = params.get("step");
  if (!focus && step) focus = step === "github" ? "repository" : step === "model" ? "models" : null;
  if (focus === ("model" as RowKey)) focus = "models";
  // Model routing is the row "Who does what" became; both names land on it.
  if (focus === ("routing" as RowKey)) focus = "jobs";
  // Older names for the rows that moved under Advanced.
  if (focus === ("github-access" as RowKey) || focus === ("github_access" as RowKey)) focus = "github_app";
  if (focus === ("ci-workflows" as RowKey)) focus = "ci";
  const add = params.get("add");
  const source = params.get("source");
  return {
    focus: focus && ROWS.includes(focus) ? focus : add || source ? "models" : null,
    add: add || null,
    source: source || null,
  };
}

/** "5m", "2h", "3d" — how long ago, for the "Checked" tooltip and age. */
export function ageLabel(at: string | null, now = Date.now()): string {
  if (!at) return "";
  const t = Date.parse(at);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.floor((now - t) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/** Whole days until `at`, never negative. */
export function daysUntil(at: string | null, now = Date.now()): number | null {
  if (!at) return null;
  const t = Date.parse(at);
  if (!Number.isFinite(t)) return null;
  return Math.max(0, Math.floor((t - now) / 86_400_000));
}

/** Which tile Add a model opens on in first run: a subscription already signed in on the
 *  server, else a local model that was found, else Claude Code's token. */
export function defaultAdd(view: SetupView): { tile: "subscription" | "api_key" | "local";
                                               provider: string } {
  const signed = view.detected.find((d) => d.kind === "subscription" && d.found === "signed_in");
  if (signed) return { tile: "subscription", provider: signed.provider };
  const local = view.detected.find((d) => d.kind === "local" && d.found === "found");
  if (local) return { tile: "local", provider: local.provider };
  return { tile: "subscription", provider: "claude_code" };
}

/** The tile a provider lives under. */
export function tileOf(view: SetupView, provider: string): "subscription" | "api_key" | "local" {
  const entry = view.catalogue.find((c) => c.provider === provider);
  return entry?.kind ?? "api_key";
}

/** Some model works, so Ask BOSS can answer (it is hidden otherwise: it could not). */
export function canAsk(view: SetupView): boolean {
  return view.models.some((m) => m.pill.tone === "green");
}

export type SetupSummary = { ready: boolean; badge: { count: number; tone: "amber" | "red" } | null };

/** The landing rule's and the rail badge's reading of the view: unfinished rows plus failing
 *  checks, red while any check fails. */
export function summarize(view: Pick<SetupView, "ready" | "next" | "problems" | "repository" | "models">):
  SetupSummary {
  const unfinished = (view.repository.repos.length ? 0 : 1) + (view.models.length ? 0 : 1);
  const count = unfinished + view.problems;
  return {
    ready: view.ready,
    badge: count > 0 ? { count, tone: view.problems > 0 ? "red" : "amber" } : null,
  };
}

/** The event the board sends so the rail badge follows the board without a reload. */
export const SETUP_BADGE_EVENT = "boss:setup-badge";
