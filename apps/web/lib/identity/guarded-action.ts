/**
 * EVERY CONSOLE ACTION DECLARES ITS PERMISSION (IDENTITY.md §6.5, R4).
 *
 *   export const publishAction = guardedAction("workflow:publish", async function publishAction(…) { … });
 *
 * The wrapper is a DECLARATION and a TRANSLATOR, never a decision: the API is the authority and
 * re-checks every call. It
 *   - records the permission on the function (permissionOf), so tests can prove every export of
 *     every *-actions.ts is declared and the UI can mirror it (lib/identity/action-permissions.ts);
 *   - lets an identity refusal from inside reach the caller in a shape it can act on: act()
 *     (lib/server-action.ts) turns `401 step_up_required` / `403 forbidden` into an ActionResult
 *     carrying `data.identity = {error, permission}`, which ActionForm reads to open the step-up
 *     sheet; a thrown refusal from an action that does not return an ActionResult is re-thrown
 *     unchanged, carrying the same `identity` field.
 *
 * Isomorphic: "use server" modules and the client-side action modules (infra, cockpit) share it.
 */
import { needsWords } from "./words";
import { isPermission, type Permission } from "./permissions.gen";

export const GUARDED = Symbol.for("boss.identity.guardedAction");

export type IdentityRefusalInfo = { error: "step_up_required" | "forbidden" | "unauthenticated"; permission: Permission | null };

/** The identity part of an API refusal body, or null. */
export function identityRefusal(status: number, body: unknown): IdentityRefusalInfo | null {
  const b = (body && typeof body === "object" ? body : {}) as { error?: unknown; permission?: unknown };
  const error = b.error;
  if ((status === 401 && (error === "step_up_required" || error === "unauthenticated")) || (status === 403 && error === "forbidden")) {
    const p = typeof b.permission === "string" && isPermission(b.permission) ? b.permission : null;
    return { error: error as IdentityRefusalInfo["error"], permission: p };
  }
  return null;
}

/** The words for an identity refusal: short, and naming the permission. */
export function identitySentence(info: IdentityRefusalInfo): string {
  if (info.error === "step_up_required") return "Confirm it's you, then try again.";
  if (info.error === "unauthenticated") return "Your session ended — sign in again.";
  return info.permission ? `${needsWords(info.permission)}.` : "Not allowed · ask an Admin.";
}

export function guardedAction<A extends unknown[], R>(permission: Permission, fn: (...args: A) => Promise<R>): (...args: A) => Promise<R> {
  if (!isPermission(permission)) throw new Error(`guardedAction: ${String(permission)} is not a catalogue permission`);
  const wrapped = async (...args: A): Promise<R> => fn(...args);
  Object.defineProperty(wrapped, GUARDED, { value: permission, enumerable: false });
  Object.defineProperty(wrapped, "name", { value: fn.name || "guardedAction" });
  return wrapped;
}

/** The permission a guarded action declared, or null for anything else. */
export function permissionOf(fn: unknown): Permission | null {
  if (typeof fn !== "function") return null;
  const p = (fn as unknown as Record<symbol, unknown>)[GUARDED];
  return typeof p === "string" && isPermission(p) ? p : null;
}

/**
 * The same declaration, IN PLACE, for an action that is already an exported function: marks it
 * with its permission without changing its identity, name or signature (so a "use server" export
 * stays exactly what Next registered). Every *-actions.ts ends with one table of these.
 */
export function declareAction<F extends (...args: never[]) => unknown>(fn: F, permission: Permission): F {
  if (!isPermission(permission)) throw new Error(`declareAction: ${String(permission)} is not a catalogue permission`);
  if (typeof fn === "function" && !(GUARDED in fn)) {
    try { Object.defineProperty(fn, GUARDED, { value: permission, enumerable: false }); } catch { /* frozen reference: the table still declares it */ }
  }
  return fn;
}
