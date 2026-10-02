/**
 * WRONG-PASSWORD BACKOFF for console sign-in: one process-wide count, a single-file gate, and
 * never a refusal.
 *
 * The console is one shared installer password guarding every key the server holds, and a wrong
 * guess used to cost nothing. The first throttle (console walk, 2026-10-02) counted misses per
 * client address read from X-Forwarded-For and refused every attempt from that address for five
 * minutes after ten misses. Review of milestone/console-b found three faults in that design:
 * the header is client-supplied when the console is reached without a rewriting proxy (Next only
 * fills it when it is absent), so a guesser changed it on each request and was never counted;
 * anyone could lock out a chosen address — the owner's — by sending ten bad guesses in its name,
 * after which even the right password was refused; and every spoofed address stayed in the Map.
 *
 * So nothing here keys on anything a client sends. Misses are counted for the whole process.
 * Past FREE_FAILURES misses in the window, every attempt waits its turn behind the previous one
 * and then a backoff delay, so guesses are serialised to at most one per delay however many
 * connections a script opens — and the right password is still checked and still signs in, it
 * just waits. The count only holds timestamps inside the window and is capped, so it cannot grow.
 */
export const FREE_FAILURES = 10;
export const FAILURE_WINDOW_MS = 5 * 60_000;
export const BASE_DELAY_MS = 500;
export const MAX_DELAY_MS = 8_000;
/** Past this many misses the delay is already at its cap; older ones need not be kept. */
const MAX_TRACKED = FREE_FAILURES + 32;

let failures: number[] = [];
let gate: Promise<void> = Promise.resolve();
let sleep: (ms: number) => Promise<void> = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function prune(now: number): number[] {
  failures = failures.filter((at) => now - at < FAILURE_WINDOW_MS).slice(-MAX_TRACKED);
  return failures;
}

/** How long the next attempt waits before its password is checked; 0 while under the limit. */
export function backoffMs(now: number): number {
  const excess = prune(now).length - FREE_FAILURES;
  if (excess < 0) return 0;
  return Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** excess);
}

export function recordFailure(now: number): void {
  failures.push(now);
  prune(now);
}

/** The right password was given: whoever was guessing did not have it, and the owner should not
 *  keep paying for their misses. */
export function recordSuccess(): void {
  failures = [];
}

/**
 * Run one password check, behind the gate when misses are past the limit. Resolves to the
 * check's result plus whether this attempt was slowed, so the form can say why it took a while.
 */
export async function throughGate<T>(now: number, check: () => Promise<T>):
  Promise<{ result: T; slowed: boolean }> {
  const delay = backoffMs(now);
  if (delay === 0) return { result: await check(), slowed: false };
  const previous = gate;
  let release!: () => void;
  gate = new Promise<void>((resolve) => { release = resolve; });
  try {
    await previous;
    await sleep(delay);
    return { result: await check(), slowed: true };
  } finally {
    release();
  }
}

/** Tests only: start from no misses and replace the real wait. */
export function resetLoginThrottleForTests(fakeSleep?: (ms: number) => Promise<void>): void {
  failures = [];
  gate = Promise.resolve();
  if (fakeSleep) sleep = fakeSleep;
}

export function trackedFailureCountForTests(): number {
  return failures.length;
}
