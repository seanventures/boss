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
 *
 * A REFRESH THAT NEVER LANDS IS UNSTUCK, NOT WAITED OUT (2026-10-10). Next 15.5's router
 * sometimes never completes a refresh issued in the first seconds after a page load: the RSC
 * response arrives whole (same bytes as a good one), React's transition lane stays suspended,
 * and every later refresh queues behind the stuck one. Measured on a seeded stack: 3 of 16
 * refreshes issued 1-2.5 s after load, 0 of 8 issued after 6 s; on the laptop install it was
 * 7 of 9 first area clicks after a load, each sitting on "Loading Factory…" until the 20 s
 * background poll. A NAVIGATION discards the stuck action at once (Next's action queue drops a
 * pending refresh for a navigate), so when a refresh has not landed within `watchdogMs` the
 * shell's `unstick` re-navigates to the current URL and refreshes again — at most
 * `maxUnsticks` times, then the ceiling stands.
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
  /** Hear `busy()` change (a refresh starting or landing): the Syncing chip subscribes, so the
   *  shell itself never re-renders for it (lib/console-refresh.ts header). */
  subscribe(listener: () => void): () => void;
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
  /** Called when the refresh in flight has landed (or was given up on at the ceiling). */
  onSettled?: () => void;
  /** Re-issue a refresh that has not landed (the shell: navigate to the same URL, refresh). */
  unstick?: () => void;
  /** How long a refresh of this kind may take before `unstick`; a function of the last
   *  refresh's measured duration. Default: "now" 2x the last (0.7-3 s), "poll" 3x the last (min 3 s). */
  watchdogMs?: (kind: RefreshKind, lastDurationMs: number) => number;
  /** How many times one refresh is unstuck before only the ceiling is left. */
  maxUnsticks?: number;
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
  let watchdog: Timer | null = null;
  let unsticks = 0;
  let disposed = false;
  const listeners = new Set<() => void>();
  const notify = () => { for (const l of [...listeners]) l(); };
  const watchdogFor = options.watchdogMs
    ?? ((kind: RefreshKind, last: number) => (kind === "now"
      ? Math.min(3_000, Math.max(700, last * 2))
      : Math.max(3_000, last * 3)));
  const maxUnsticks = options.maxUnsticks ?? 3;
  let generation = 0;

  const merge = (a: RefreshKind | null, b: RefreshKind): RefreshKind =>
    a === "now" || b === "now" ? "now" : "poll";

  function arm(kind: RefreshKind) {
    if (!options.unstick) return;
    if (watchdog !== null) clearTimer(watchdog);
    watchdog = setTimer(() => {
      watchdog = null;
      if (!inFlight || disposed || unsticks >= maxUnsticks) return;
      unsticks += 1;
      options.unstick!();
      arm(kind);
    }, watchdogFor(kind, lastDuration) * (1 + unsticks));
  }

  function fire(kind: RefreshKind = "poll") {
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
    unsticks = 0;
    options.start();
    arm(kind);
    notify();
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
            fire("poll");
          }, wait);
        }
        return;
      }
    }
    fire(kind);
  }

  function landed() {
    if (!inFlight) return;
    inFlight = false;
    if (ceiling !== null) { clearTimer(ceiling); ceiling = null; }
    if (watchdog !== null) { clearTimer(watchdog); watchdog = null; }
    landedAt = now();
    lastDuration = landedAt - startedAt;
    options.onSettled?.();
    notify();
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
    if (watchdog !== null) { clearTimer(watchdog); watchdog = null; }
    if (!disposed) notify();
  }

  return {
    request,
    landed,
    cancel,
    busy: () => inFlight,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
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
