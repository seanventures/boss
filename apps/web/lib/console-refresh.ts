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
  /** How long this server took to render the document the console loaded with (navigation
   *  timing), before any refresh has been measured. A refresh is never unstuck before 1.5x
   *  this: a slow server's first refresh is still being answered, and restarting it only
   *  makes the server render it again (rc9 merge, scratchpad/rc9/MERGE.md). 0 = unknown. */
  serverMs?: number;
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
  const floor = Math.max(700, (options.serverMs ?? 0) * 1.5);
  const watchdogFor = options.watchdogMs
    ?? ((kind: RefreshKind, last: number) => (kind === "now"
      ? Math.min(Math.max(3_000, floor), Math.max(floor, last * 2))
      : Math.max(3_000, floor, last * 3)));
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

// --------------------------------------------------------------------------- //
// EVENT-DRIVEN REFRESH (BOSSAPP-835). The API pushes every event the viewer may see on
// GET /v1/events/stream (authorized per event, server side). A matching event invalidates the
// keys the console polls — here, the areas whose server render shows that kind — and asks the
// coordinator for ONE refresh (coalesced like any other; a burst of events is one refresh).
// POLLING STAYS: this only makes a change show sooner. A stream that drops or never connects
// leaves the console exactly as it was before this existed.

/** One pushed event, as the stream sends it (the fields this module reads). */
export type ConsoleEvent = {
  id: string;
  event_type: string;
  project_id?: string;
  entity_type?: string;
  entity_id?: string;
};

/** Event kind prefix -> the polled keys (console areas) whose render shows it. "*" = any area. */
export const EVENT_INVALIDATES: ReadonlyArray<readonly [string, readonly string[]]> = [
  ["decision.", ["decisions", "overview", "dashboard", "project", "projects"]],
  ["chat.decision_answered", ["decisions", "overview", "dashboard", "project"]],
  ["code_task.", ["code_tasks", "project", "projects", "overview", "dashboard", "activity"]],
  ["workflow.", ["delivery_workflows", "project", "overview", "activity"]],
  ["meeting.", ["meetings", "project"]],
  ["discovery.", ["discovery"]],
  ["deploy", ["delivery", "project", "infra"]],
  ["preview.", ["code_tasks", "project", "delivery"]],
  ["incident.", ["ai-sre", "monitoring", "observability", "reliability"]],
  ["alert.", ["monitoring", "observability", "ai-sre"]],
  ["self_fix.", ["self_fixes"]],
  ["work_item.", ["work", "agent_ops"]],
  ["budget.", ["budget"]],
];

/** The polled keys an event invalidates. Every event invalidates the Activity log. */
export function invalidatedKeys(eventType: string): string[] {
  const keys = new Set<string>(["activity"]);
  for (const [prefix, areas] of EVENT_INVALIDATES) {
    if (eventType === prefix || eventType.startsWith(prefix)) for (const a of areas) keys.add(a);
  }
  return [...keys];
}

/** What the subscription needs from EventSource (the browser's, or a test's fake). */
export type EventSourceLike = {
  onmessage: ((ev: { data: string; lastEventId?: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  addEventListener(type: string, listener: (ev: { data: string }) => void): void;
  close(): void;
};

export type EventSubscriptionOptions = {
  /** Open the stream (the shell: `new EventSource(url)`). */
  open: () => EventSourceLike;
  /** The polled keys on screen now (the shell: the active area). */
  watching: () => readonly string[];
  /** Invalidate: ask for one refresh (the shell: coordinator.request("poll")). */
  invalidate: (reason: { kind: "event" | "resync"; event?: ConsoleEvent; keys: string[] }) => void;
  /** Coalesce a burst into one invalidation. Default 400 ms. */
  debounceMs?: number;
  setTimer?: (fn: () => void, ms: number) => Timer;
  clearTimer?: (t: Timer) => void;
};

export type EventSubscription = {
  close(): void;
  /** Events seen / invalidations asked for (the Syncing chip and tests read these). */
  stats(): { events: number; invalidations: number; resyncs: number };
};

export function createEventSubscription(options: EventSubscriptionOptions): EventSubscription {
  const setTimer = options.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = options.clearTimer ?? ((t) => clearTimeout(t as ReturnType<typeof setTimeout>));
  const debounceMs = options.debounceMs ?? 400;
  const counts = { events: 0, invalidations: 0, resyncs: 0 };
  let pending: { kind: "event" | "resync"; event?: ConsoleEvent; keys: Set<string> } | null = null;
  let timer: Timer | null = null;
  let closed = false;

  const flush = () => {
    timer = null;
    if (closed || !pending) return;
    const reason = { kind: pending.kind, event: pending.event, keys: [...pending.keys] };
    pending = null;
    counts.invalidations += 1;
    options.invalidate(reason);
  };
  const owe = (kind: "event" | "resync", keys: string[], event?: ConsoleEvent) => {
    if (!pending) pending = { kind, event, keys: new Set() };
    if (kind === "resync") pending.kind = "resync";
    for (const k of keys) pending.keys.add(k);
    if (timer === null) timer = setTimer(flush, debounceMs);
  };

  const source = options.open();
  source.onmessage = (msg) => {
    if (closed) return;
    let event: ConsoleEvent;
    try { event = JSON.parse(msg.data) as ConsoleEvent; } catch { return; }
    if (!event || typeof event.event_type !== "string") return;
    counts.events += 1;
    const keys = invalidatedKeys(event.event_type);
    const watching = options.watching();
    if (keys.some((k) => watching.includes(k))) owe("event", keys, event);
  };
  // The server lost track (a slow reader overflowed, or its listener reconnected): whatever is
  // on screen may be stale, so it is refetched once.
  source.addEventListener("resync", () => {
    if (closed) return;
    counts.resyncs += 1;
    owe("resync", [...options.watching()]);
  });
  // EventSource reconnects by itself, sending Last-Event-ID; nothing to do but keep polling.
  source.onerror = () => {};

  return {
    close() {
      closed = true;
      if (timer !== null) { clearTimer(timer); timer = null; }
      source.close();
    },
    stats: () => ({ ...counts }),
  };
}

/** The console's stream URL (same origin: app/api/events/stream proxies it with the session). */
export function consoleEventsUrl(projectId?: string | null): string {
  const q = new URLSearchParams();
  if (projectId) q.set("project_id", projectId);
  const s = q.toString();
  return `/api/events/stream${s ? `?${s}` : ""}`;
}
