/**
 * WORKING TIME, NOT WAITING TIME — the one place a workflow step's clock is read.
 *
 * Operator, 2026-11-06: a Deploy step waiting for a person's approval showed "6h 50m…" on the
 * rail and on its card, exactly as a step that had been working for seven hours would. The old
 * clock (delivery-workflows-ui `stepDuration`) knew one kind of wait — a step's own gate, by
 * `decision_approved_at` — and nothing else: a stack deployment awaiting approval (its decision
 * lives on the deployment, not the step), a pull request waiting to be merged, an architecture
 * waiting for its reviewer all ticked on as "working".
 *
 * Now every surface asks `stepClock` and gets one of two things:
 *   - a step WAITING ON A PERSON says so — "waiting on you · since 3:23 PM" — and never a timer;
 *   - otherwise the step's WORKING time, with every interval it spent waiting on a person
 *     (a gate before it was answered, a stack deployment before it was approved) left out.
 * tests/a-step-waiting-on-you-is-not-a-timer.test.ts fails if a step's time is formatted anywhere
 * else in the workflow view.
 */
import { humanDuration } from "./datetime";

/** The minimal step shape the clock reads (delivery-workflows-ui's RunStep satisfies it). */
export type ClockStep = {
  status: string;
  output?: Record<string, unknown> | null;
  started_at?: string | null;
  completed_at?: string | null;
  decision_id?: string | null;
  decision_status?: string | null;
  decision_approved_at?: string | null;
  stack_deploy?: { deployment?: Record<string, unknown> | null } | null;
};

/** Gate states that can still be answered. Anything else has been decided or has lapsed. */
export const OPEN_GATE = new Set(["pending", "delegated"]);

/** Stack deployment states that are a person's to move (the approval is theirs). */
const STACK_WAITING = new Set(["awaiting_approval"]);

/** Work a step awaits that is a PERSON's to finish, by the step's `output.await.kind`. */
const PERSON_AWAITS = new Set(["merge", "architecture"]);

function awaitKind(step: ClockStep): string {
  const awaited = (step.output ?? {}).await as { kind?: unknown } | undefined;
  return awaited && typeof awaited.kind === "string" ? awaited.kind : "";
}

/** A deploy held until its pull request is merged (runner._merge_wait). Its question may
 *  already be approved — BOSS does not merge on every install — and the merge is still a
 *  person's to do, so it needs them until GitHub shows it merged. */
export function waitsForMerge(step: ClockStep): boolean {
  return step.status === "waiting" && awaitKind(step) === "merge";
}

/** A step needing a person to answer its gate (or merge its PR), right now.
 *
 *  THE GATE'S STATE, NOT ONLY THE STEP'S. This was `decision_id && status === "waiting"`, which
 *  is true for a gate nobody has answered AND for one answered thirty seconds ago — the step
 *  stays `waiting` until a worker picks the answer up. Unknown (an older API, or a decision row
 *  that has gone) keeps the old behaviour: offering an answerable gate is recoverable, hiding
 *  one is not. */
export function needsYou(step: ClockStep): boolean {
  if (waitsForMerge(step)) return true;
  if (!step.decision_id || step.status !== "waiting") return false;
  return !step.decision_status || OPEN_GATE.has(step.decision_status);
}

function stackDeployment(step: ClockStep): Record<string, unknown> | null {
  const d = step.stack_deploy?.deployment;
  return d && typeof d === "object" ? d : null;
}

function at(value: unknown): number {
  return typeof value === "string" && value ? Date.parse(value) : NaN;
}

/**
 * Whether the step is, right now, waiting on a PERSON — and since when (ms, or NaN unknown).
 * Null when it is not. Every kind of person-wait is listed here and nowhere else.
 */
export function personWait(step: ClockStep): { since: number } | null {
  if (step.completed_at || step.status !== "waiting") return null;
  const started = at(step.started_at);
  if (needsYou(step)) return { since: started };
  const deployment = stackDeployment(step);
  if (deployment) {
    return STACK_WAITING.has(String(deployment.status ?? ""))
      ? { since: Number.isFinite(at(deployment.created_at)) ? at(deployment.created_at) : started }
      : null;
  }
  if (awaitKind(step) === "stack_deployment") return { since: started };
  if (PERSON_AWAITS.has(awaitKind(step))) return { since: started };
  return null;
}

/** The intervals [from, to) the step spent waiting on a person, as far as the record says. */
function waitedIntervals(step: ClockStep, end: number): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  const started = at(step.started_at);
  // A step's own gate: raised when the step started, answered at `decision_approved_at`.
  const approved = at(step.decision_approved_at);
  if (Number.isFinite(approved)) out.push([started, approved]);
  // A stack deployment: planned at `created_at`, applied (the approval landed) at
  // `apply_started_at`; still unapplied means it is still waiting.
  const deployment = stackDeployment(step);
  const planned = at(deployment?.created_at);
  if (deployment && Number.isFinite(planned)) {
    const applied = at(deployment.apply_started_at);
    out.push([planned, Number.isFinite(applied) ? applied : end]);
  }
  return out;
}

/** Seconds of WORK between start and end: the span minus every person-wait (overlaps merged). */
export function workingSeconds(step: ClockStep, nowMs: number = Date.now()): number | null {
  const start = at(step.started_at);
  const end = step.completed_at ? at(step.completed_at) : nowMs;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  const waits = waitedIntervals(step, end)
    .map(([a, b]) => [Math.max(a, start), Math.min(b, end)] as [number, number])
    .filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && b > a)
    .sort((x, y) => x[0] - y[0]);
  let waited = 0;
  let reach = start;
  for (const [a, b] of waits) {
    const from = Math.max(a, reach);
    if (b > from) { waited += b - from; reach = b; }
  }
  return (end - start - waited) / 1000;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "3:23 PM" today, "Nov 5" on an earlier day — UTC, like every time the console prints. */
export function sinceWords(sinceMs: number, nowMs: number): string {
  const d = new Date(sinceMs);
  const now = new Date(nowMs);
  if (d.getUTCFullYear() !== now.getUTCFullYear() || d.getUTCMonth() !== now.getUTCMonth()
      || d.getUTCDate() !== now.getUTCDate()) {
    return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
  }
  const h = d.getUTCHours() % 12 || 12;
  return `${h}:${String(d.getUTCMinutes()).padStart(2, "0")} ${d.getUTCHours() >= 12 ? "PM" : "AM"}`;
}

export type StepClock =
  /** `short` is the rail's line ("needs you · 3:23 PM", fits a node); `text` the step card's. */
  | { kind: "waiting"; text: string; short: string; title: string }
  | { kind: "working"; text: string; title: string }
  | null;

/**
 * What a step's clock says: "waiting on you · since 3:23 PM" while a person holds it, else its
 * working time ("4m 20s", "4m 20s…" while it still works). Null when there is nothing to say.
 */
export function stepClock(step: ClockStep, nowMs: number = Date.now()): StepClock {
  const wait = personWait(step);
  if (wait) {
    if (!Number.isFinite(wait.since)) {
      return { kind: "waiting", text: "waiting on you", short: "needs you",
               title: "Waiting for a person — not working time" };
    }
    const iso = new Date(wait.since).toISOString().replace("T", " ").slice(0, 16);
    const since = sinceWords(wait.since, nowMs);
    return { kind: "waiting", text: `waiting on you · since ${since}`, short: `needs you · ${since}`,
             title: `Waiting for a person since ${iso} UTC — not counted as working time` };
  }
  if (!step.started_at) return null;
  const seconds = workingSeconds(step, nowMs);
  if (seconds === null) return null;
  const text = humanDuration(seconds);
  if (!text) return null;
  return step.completed_at
    ? { kind: "working", text, title: "Working time (time waiting on a person is not counted)" }
    : { kind: "working", text: `${text}…`, title: "Working so far" };
}
