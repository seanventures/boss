"use server";

/**
 * The server half of lib/call-action.ts: run a server action under the press that invoked it.
 *
 * A server action called from code (`await deployTargetAction(id, ref)`) has no form, so the
 * SubmitButton's per-press `__boss_intent` never reached the server, and its API writes went out
 * with no Idempotency-Key — a double-pressed Deploy or a duplicated chat turn (2026-09-10) was
 * guarded by nothing but a `busy` flag that lands after React re-renders. The browser cannot add
 * a header to a server action's request, and appending the press to the action's own arguments
 * would land it in whatever optional parameter came next (`deployTargetAction(id, ref,
 * planOnly)`). So the press travels to THIS action, first, beside a reference to the real one:
 * middleware.ts lifts `__boss_intent` out of the request body into `x-boss-intent` exactly as it
 * does for a form, and every apiWrite the real action makes in this same request derives its
 * key from it. The real action receives its arguments untouched.
 *
 * Invoking a reference the client passed grants nothing new: the client could invoke that same
 * action directly, and React resolves the reference only against the app's own action manifest.
 */
export async function runPressedAction<A extends unknown[], R>(
  press: { __boss_intent: string },
  action: (...args: A) => Promise<R>,
  ...args: A
): Promise<R> {
  void press; // consumed by middleware.ts from the request body, before this runs
  return action(...args);
}
