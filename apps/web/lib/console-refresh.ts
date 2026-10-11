/**
 * ONE REFRESH AT A TIME, FOR THE WHOLE CONSOLE. Every server re-render goes through here.
 *
 * Why a coordinator and not `router.refresh()` wherever a panel wants fresh data:
 *
 *   * Next.js chains refreshes. A refresh asked for while another is in flight is queued
 *     behind it, never dropped — so a panel polling every 4 seconds against a render that costs
 *     20 seconds grows the queue without bound.
 *   * React renders every pending transition together. The router's state is the NEWEST
 *     refresh's promise, so nothing commits until the newest one lands — and by then the next
 *     poll has queued another. While refreshes were asked for faster than they completed,
 *     nothing on screen changed at all.
 *
 * That is what "Loading Discovery… taking longer than usual" was on boss-1 (2026-10-04): the
 * server rendered the Discovery area four times, 16-48 s apart, one refresh after another,
 * and the browser never committed any of them, because a panel already on screen (a live code
 * task, a running discovery, a promotion in flight — each a bare 4-5 s `setInterval` of
 * `router.refresh()`) kept the queue full.
 *
 * So: at most ONE current logical refresh, at most ONE more owed. Asking while one is in flight
 * marks one owed; it starts the moment the in-flight one lands, so it sees everything asked
 * for meanwhile (an area added to the session cookie, a change just made). A poll also waits
 * out a cool-down as long as the last refresh took, so a slow console polls itself gently; a
 * "now" (an area being opened, a change the operator just made) skips the cool-down but still
 * never overlaps another refresh the router still accepts.
 *
 * Pure: the shell supplies `start` and reports `landed()` when fresh server props commit.
 * A committed URL restore can discard Next's pending response; the shell then calls `cancel()`
 * before requesting the current area. Cancellation releases bookkeeping, not the network request.
 * Timers and the clock are injectable, so it is tested for real.
 */

export type RefreshKind = "poll" | "now";

export type RefreshCoordinator = {
  /** Ask for a refresh. Coalesced while another logical refresh is in flight. */
  request(kind?: RefreshKind): void;
  /** The shell reports newly committed server props for the current refresh. A canceled
   *  response must not acknowledge a replacement request. */
  landed(): void;
  /** Release a discarded refresh and pending work without recording success or aborting I/O. */
  cancel(): void;
  /** A logical refresh is in flight. */
  busy(): boolean;
  /** How long the last refresh took, start to landing. 0 before the first. */
  lastDurationMs(): number;
  dispose(): void;
};

type Timer = unknown;
export type CoordinatorOptions = {
  start: () => void;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => Timer;
  clearTimer?: (t: Timer) => void;
  /** A refresh that never reports landing is treated as landed after this long, so one lost
   *  report (a refresh Next discarded for a navigation) cannot stop the console refreshing. */
  ceilingMs?: number;
  /** The longest a poll waits after the last refresh. */
  maxCooldownMs?: number;
};

export function createRefreshCoordinator(options: CoordinatorOptions): RefreshCoordinator {
  const now = options.now ?? (() => Date.now());
  const setTimer = options.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = options.clearTimer ?? ((t) => clearTimeout(t as ReturnType<typeof setTimeout>));
  const ceilingMs = options.ceilingMs ?? 120_000;
  const maxCooldownMs = options.maxCooldownMs ?? 60_000;

  let inFlight = false;
  let startedAt = 0;
  let landedAt = -Infinity;
  let lastDuration = 0;
  let owed: RefreshKind | null = null;
  let cooldown: Timer | null = null;
  let ceiling: Timer | null = null;
  let disposed = false;
  let generation = 0;

  const merge = (a: RefreshKind | null, b: RefreshKind): RefreshKind =>
    a === "now" || b === "now" ? "now" : "poll";

  function fire() {
    if (disposed) return;
    if (cooldown !== null) { clearTimer(cooldown); cooldown = null; }
    inFlight = true;
    owed = null;
    startedAt = now();
    const attempt = ++generation;
    ceiling = setTimer(() => {
      if (disposed || attempt !== generation) return;
      ceiling = null;
      landed();
    }, ceilingMs);
    options.start();
  }

  function request(kind: RefreshKind = "poll") {
    if (disposed) return;
    if (inFlight) { owed = merge(owed, kind); return; }
    if (kind === "poll") {
      const wait = Math.min(maxCooldownMs, lastDuration) - (now() - landedAt);
      if (wait > 0) {
        owed = merge(owed, kind);
        if (cooldown === null) {
          const scheduled = generation;
          cooldown = setTimer(() => {
            if (disposed || scheduled !== generation) return;
            cooldown = null;
            fire();
          }, wait);
        }
        return;
      }
    }
    fire();
  }

  function landed() {
    if (!inFlight) return;
    inFlight = false;
    if (ceiling !== null) { clearTimer(ceiling); ceiling = null; }
    landedAt = now();
    lastDuration = landedAt - startedAt;
    if (owed !== null) {
      const kind = owed;
      owed = null;
      request(kind);
    }
  }

  function cancel() {
    generation += 1;
    inFlight = false;
    owed = null;
    if (cooldown !== null) { clearTimer(cooldown); cooldown = null; }
    if (ceiling !== null) { clearTimer(ceiling); ceiling = null; }
  }

  return {
    request,
    landed,
    cancel,
    busy: () => inFlight,
    lastDurationMs: () => lastDuration,
    dispose() {
      disposed = true;
      cancel();
    },
  };
}

// --------------------------------------------------------------------------- //
// The console's one coordinator. The shell registers it; panels ask through it.

let active: RefreshCoordinator | null = null;

/** The shell installs its coordinator; the return value uninstalls it. */
export function registerConsoleRefresh(coordinator: RefreshCoordinator): () => void {
  active = coordinator;
  return () => { if (active === coordinator) active = null; };
}

/**
 * Ask for a server re-render. Through the console's coordinator when the shell is mounted;
 * a page with no shell (the org gate, the reconnect screen) has nothing to collide with, and
 * refreshes directly.
 */
export function requestConsoleRefresh(router: { refresh(): void }, kind: RefreshKind = "poll"): void {
  if (active) active.request(kind);
  else router.refresh();
}
