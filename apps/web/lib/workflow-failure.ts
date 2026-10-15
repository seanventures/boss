/**
 * What a step does when it fails (#512 phase 2): how many times it is tried, how long it waits
 * between tries, how long one try may run, and where the run goes when it still fails.
 *
 * THE CONTRACT IS THE ENGINE'S, every field optional on a step:
 *
 *     max_attempts      int 1..25, default 3
 *     retry_backoff     { initial_seconds: int 0..3600 (default 0),
 *                         multiplier: number 1..10 (default 2),
 *                         max_seconds: int 0..86400 (default 3600) }
 *                       the wait before try n+1 is min(initial × multiplier^(n-1), max)
 *     timeout_seconds   int 1..86400; a try that runs longer fails (`step_timeout`)
 *     on_error          a step id: when the step fails for good, the run goes on there
 *                       (event `workflow.error_path_taken`) instead of failing
 *
 * NOTHING IS WRITTEN THAT WAS NOT SET. A field left empty is omitted from the spec, not written
 * as its default, so a spec says only what its author chose and the engine's defaults stay the
 * engine's. The ranges here only stop a value the engine would refuse from being typed in; the
 * engine validator (POST check) is still the source of truth for the graph: whether `on_error`
 * makes a cycle, or leaves a step unreachable, is never decided here.
 */
import type { SpecStep } from "./workflow-editor-types";

type Obj = Record<string, unknown>;
const asObj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});

export const DEFAULT_ATTEMPTS = 3;
export const BACKOFF_DEFAULTS = { initial_seconds: 0, multiplier: 2, max_seconds: 3600 } as const;

/** Each number a step's failure settings hold, with the range the engine accepts. */
export const FAILURE_RANGES = {
  max_attempts: { min: 1, max: 25, whole: true },
  initial_seconds: { min: 0, max: 3600, whole: true },
  multiplier: { min: 1, max: 10, whole: false },
  max_seconds: { min: 0, max: 86400, whole: true },
  timeout_seconds: { min: 1, max: 86400, whole: true },
} as const;
export type FailureNumber = keyof typeof FAILURE_RANGES;

export type RetryBackoff = { initial_seconds?: number; multiplier?: number; max_seconds?: number };
export type StepFailure = {
  maxAttempts: number | null;
  backoff: RetryBackoff;
  timeoutSeconds: number | null;
  onError: string | null;
};

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** A step's failure settings as written (null / absent where unset). */
export function failureOf(step: SpecStep | Obj): StepFailure {
  const raw = asObj(step.retry_backoff);
  const backoff: RetryBackoff = {};
  for (const key of ["initial_seconds", "multiplier", "max_seconds"] as const) {
    const n = num(raw[key]);
    if (n !== null) backoff[key] = n;
  }
  const onError = typeof step.on_error === "string" && step.on_error ? step.on_error : null;
  return { maxAttempts: num(step.max_attempts), backoff, timeoutSeconds: num(step.timeout_seconds), onError };
}

/** A number typed into one of the fields, as the value to store or why it cannot be.
 *  Empty is `null`: the field is unset. */
export function readNumber(field: FailureNumber, text: string): { value: number | null } | { error: string } {
  const t = text.trim();
  if (!t) return { value: null };
  const n = Number(t);
  const range = FAILURE_RANGES[field];
  if (!Number.isFinite(n)) return { error: "Enter a number" };
  if (range.whole && !Number.isInteger(n)) return { error: "Enter a whole number" };
  if (n < range.min || n > range.max) return { error: `Enter ${range.min} to ${range.max}` };
  return { value: n };
}

/** "10s", "2m", "1m 30s", "1h", "2h 5m", "1d": a number of seconds as people say it. */
export function saySeconds(total: number): string {
  const s = Math.max(0, Math.round(total));
  if (s < 60) return `${s}s`;
  if (s < 3600) return s % 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s / 60}m`;
  if (s < 86400) {
    const m = Math.floor((s % 3600) / 60);
    return m ? `${Math.floor(s / 3600)}h ${m}m` : `${Math.floor(s / 3600)}h`;
  }
  const h = Math.floor((s % 86400) / 3600);
  return h ? `${Math.floor(s / 86400)}d ${h}h` : `${Math.floor(s / 86400)}d`;
}

/** The waits between tries, in seconds, with the engine's defaults for what is unset:
 *  min(initial × multiplier^(n-1), max) before try n+1. One fewer than the tries. */
export function backoffWaits(attempts: number | null, backoff: RetryBackoff): number[] {
  const tries = Math.max(1, Math.min(FAILURE_RANGES.max_attempts.max, Math.round(attempts ?? DEFAULT_ATTEMPTS)));
  const initial = backoff.initial_seconds ?? BACKOFF_DEFAULTS.initial_seconds;
  const multiplier = backoff.multiplier ?? BACKOFF_DEFAULTS.multiplier;
  const max = backoff.max_seconds ?? BACKOFF_DEFAULTS.max_seconds;
  const out: number[] = [];
  for (let n = 1; n < tries; n += 1) out.push(Math.min(initial * multiplier ** (n - 1), max));
  return out;
}

/** The waits in plain words: "waits 10s, then 20s, then 40s". */
export function sayWaits(attempts: number | null, backoff: RetryBackoff): string {
  const waits = backoffWaits(attempts, backoff);
  if (!waits.length) return "Tried once: no retries";
  if (waits.every((w) => w === 0)) {
    return waits.length === 1 ? "Tries again straight away" : `Tries again straight away, ${waits.length} times`;
  }
  const said = waits.map((w) => (w === 0 ? "no wait" : saySeconds(w)));
  if (said.length <= 4) return `Waits ${said.join(", then ")}`;
  return `Waits ${said.slice(0, 3).join(", then ")}, … then ${said[said.length - 1]}`;
}

/** The units the time limit is typed in. */
export const TIME_UNITS = [
  { key: "s", label: "seconds", seconds: 1 },
  { key: "m", label: "minutes", seconds: 60 },
  { key: "h", label: "hours", seconds: 3600 },
] as const;
export type TimeUnit = (typeof TIME_UNITS)[number]["key"];

/** The largest unit a stored number of seconds is a whole number of. */
export function unitFor(seconds: number | null): TimeUnit {
  if (seconds && seconds % 3600 === 0) return "h";
  if (seconds && seconds % 60 === 0) return "m";
  return seconds ? "s" : "m";
}

export const unitSeconds = (unit: TimeUnit): number => TIME_UNITS.find((u) => u.key === unit)!.seconds;

/** A time limit typed as `text` `unit`s, as the seconds to store or why it cannot be. */
export function readTimeLimit(text: string, unit: TimeUnit): { value: number | null } | { error: string } {
  const t = text.trim();
  if (!t) return { value: null };
  const n = Number(t);
  if (!Number.isFinite(n) || n <= 0) return { error: "Enter a time above zero" };
  const exact = n * unitSeconds(unit);
  const seconds = Math.round(exact);
  if (Math.abs(seconds - exact) > 1e-6) return { error: "Enter a whole number of seconds" };
  const { min, max } = FAILURE_RANGES.timeout_seconds;
  if (seconds < min || seconds > max) return { error: "Enter 1 second to 24 hours" };
  return { value: seconds };
}

/** `retry_backoff` with one key set (or unset), dropped altogether when nothing is left. */
export function withBackoff(step: SpecStep, key: keyof RetryBackoff, value: number | null): RetryBackoff | null {
  const next: Obj = { ...asObj(step.retry_backoff) };
  if (value === null) delete next[key];
  else next[key] = value;
  return Object.keys(next).length ? (next as RetryBackoff) : null;
}

/** What the node's badge says, or null when the step fails the default way (3 tries, no wait,
 *  no time limit). The error path has an arrow of its own. */
export function failureBadge(step: SpecStep | Obj): string | null {
  const f = failureOf(step);
  const parts: string[] = [];
  if (f.maxAttempts !== null && f.maxAttempts > DEFAULT_ATTEMPTS) parts.push(`Tried up to ${f.maxAttempts} times`);
  if (f.timeoutSeconds !== null) parts.push(`Time limit ${saySeconds(f.timeoutSeconds)}`);
  return parts.length ? parts.join(" · ") : null;
}
