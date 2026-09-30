/**
 * One signal, so a change made in one place shows up in the others.
 *
 * The console renders every area into a single document and shows one at a time, and each area
 * polls on its own schedule. So a target set on the Factory card, a step re-queued from the
 * Workflows screen, and an action run by BOSS in the dock all changed the same rows and left
 * the other two panels showing what they had already fetched — for as long as their own
 * interval took, or for ever if they had stopped polling.
 *
 * The operator's summary: "if operation is done through Workflow or Factory page or through
 * BOSS agent it should be updated all places — it does not matter where, but other places
 * should reflect the changes".
 *
 * Deliberately the smallest thing that can work. No store, no shared cache, no coordination:
 * a window event that says "something changed", and each panel reloads the way it already
 * knows how. Panels stay independent, and a panel that has not subscribed is exactly as
 * correct as it was before.
 */
const EVENT = "boss:changed";

/** What changed, for debugging and for a panel that wants to ignore its own echo. */
export type ChangeNote = { source: string; detail?: Record<string, unknown> };

/**
 * Where the signal travels. `window` in a browser; injectable so the behaviour above can be
 * tested for real rather than asserted about by reading the source — this suite has no DOM,
 * and a mechanism nothing exercises is a mechanism nobody knows works.
 */
function bus(target?: EventTarget): EventTarget | null {
  if (target) return target;
  return typeof window === "undefined" ? null : window;
}

/** Say that something changed. Safe to call from anywhere, including the server. */
export function notifyChanged(
  source: string, detail?: Record<string, unknown>, target?: EventTarget,
): void {
  const where = bus(target);
  if (!where) return;
  try {
    where.dispatchEvent(new CustomEvent<ChangeNote>(EVENT, { detail: { source, detail } }));
  } catch {
    // A notification that throws must never break the action that succeeded.
  }
}

/**
 * Reload when something changed somewhere else.
 *
 * `ignore` is the caller's own source name: a panel that reloads on its own actions already
 * does so through its normal path, and reacting to its own echo would double every request.
 */
export function onChanged(
  handler: (note: ChangeNote) => void,
  ignore?: string,
  target?: EventTarget,
): () => void {
  const where = bus(target);
  if (!where) return () => {};
  const listener = (event: Event) => {
    const note = (event as CustomEvent<ChangeNote>).detail;
    if (!note || (ignore && note.source === ignore)) return;
    handler(note);
  };
  where.addEventListener(EVENT, listener);
  return () => where.removeEventListener(EVENT, listener);
}
