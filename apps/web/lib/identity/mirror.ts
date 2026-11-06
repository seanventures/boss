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
  /** The project the check mirrors (lib/identity/read-gates.ts judges only calls naming it). */
  projectId?: string | null;
};

const KEY = "__bossIdentityMirror";

export function installMirror(mirror: Mirror | null): void {
  (globalThis as Record<string, unknown>)[KEY] = mirror;
}

export function currentMirror(): Mirror | null {
  return ((globalThis as Record<string, unknown>)[KEY] as Mirror | null | undefined) ?? null;
}

/**
 * THE PROVIDERS THAT ARE REALLY ON THE PAGE, for the step-up sheet.
 *
 * The mirror above is installed DURING RENDER (so a child's mount effect already sees it), which
 * means it can belong to a provider React rendered and then threw away — a navigation that was
 * interrupted, a subtree that failed and fell back. Its stepUp put the ask into a component that
 * never mounted: no "Confirm it's you" appeared, the write's promise never settled, and the
 * panel stayed busy, so every later press of Approve went nowhere (boss-1, 2026-11-06, a Deploy
 * step's stack approval: one `401 step_up_required` in the API log, then silence). A provider
 * that unmounted while another stayed also nulled the mirror for the whole page.
 *
 * A provider registers its sheet here when it MOUNTS and leaves when it unmounts; a step-up asked
 * of a provider that is not mounted is passed to the most recently mounted one that is, and with
 * none it is answered "not confirmed" at once — never left waiting.
 */
/** A mounted provider's entry: `current` always holds that provider's latest mirror. */
export type LiveMirror = { current: Mirror };
const LIVE = "__bossIdentityLiveMirrors";

function liveList(): LiveMirror[] {
  const g = globalThis as Record<string, unknown>;
  if (!Array.isArray(g[LIVE])) g[LIVE] = [];
  return g[LIVE] as LiveMirror[];
}

/** Register a MOUNTED provider; returns what its unmount calls. Leaving, it hands the installed
 *  mirror to the provider still on the page (or to none), never nulling another's. */
export function registerLiveMirror(entry: LiveMirror): () => void {
  liveList().push(entry);
  return () => {
    const list = liveList();
    const at = list.lastIndexOf(entry);
    if (at >= 0) list.splice(at, 1);
    if (currentMirror() === entry.current) installMirror(liveMirror());
  };
}

/** The mirror of the most recently mounted provider still on the page, or null. */
export function liveMirror(): Mirror | null {
  const list = liveList();
  return list.length ? list[list.length - 1].current : null;
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
