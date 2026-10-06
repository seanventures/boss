/**
 * THE MIRROR, FOR CODE THAT IS NOT A COMPONENT. lib/call-action.ts invokes server actions from
 * event handlers, where no hook can reach the identity provider; the provider registers its
 * check, step-up and refresh here when it mounts (components/identity/identity-provider.tsx).
 * Without a provider (a unit test) nothing is mirrored and every call simply goes to the API.
 */
import type { CanResult } from "./contract";
import type { Permission } from "./permissions.gen";

export type Mirror = {
  check: (permission: Permission) => CanResult;
  stepUp: (reason: string) => Promise<boolean>;
  refresh: () => Promise<void>;
};

const KEY = "__bossIdentityMirror";

export function installMirror(mirror: Mirror | null): void {
  (globalThis as Record<string, unknown>)[KEY] = mirror;
}

export function currentMirror(): Mirror | null {
  return ((globalThis as Record<string, unknown>)[KEY] as Mirror | null | undefined) ?? null;
}

/** An ActionResult-like value that says the API wants a fresh second factor. */
export function wantsStepUp(value: unknown): boolean {
  const v = value as { ok?: unknown; data?: { identity?: { error?: unknown } } } | null;
  return !!v && v.ok === false && v.data?.identity?.error === "step_up_required";
}

/** An ActionResult-like value that says the viewer lacks a permission (the mirror is stale). */
export function wasForbidden(value: unknown): boolean {
  const v = value as { ok?: unknown; data?: { identity?: { error?: unknown } } } | null;
  return !!v && v.ok === false && v.data?.identity?.error === "forbidden";
}
