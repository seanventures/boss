/**
 * The doors into "add a feature to an app you already have" (the composer on branch np/compose
 * owns the flow; these only link to it). One place, so the target moves in one edit.
 *
 *   area COMPOSE_AREA, compose=add            — pick the app there
 *   area COMPOSE_AREA, compose=add, app=<key> — that app, already chosen
 *
 * NOTE: COMPOSE_AREA becomes a shell area when np/compose lands; until then these doors open
 * an area the shell does not know (the operator's call, 2026-10-05).
 */
export const COMPOSE_AREA = "delivery_workflows";

export function addFeaturePatch(appKey?: string | null): Record<string, string | null> {
  return { area: COMPOSE_AREA, compose: "add", app: appKey ?? null, view: null, role: null, build: null };
}

/** Every "New build" door's tooltip. Each door is a GatedButton on workflow_run:start, what the
 *  composer's Start sends (POST /v1/delivery-workflows/start). */
export const NEW_BUILD_TIP = "Start a new app or add to an existing one";

/** The New project composer, "A new app" chosen; its mode switch offers "Add to an existing app". */
export function newProjectPatch(): Record<string, string | null> {
  return { area: COMPOSE_AREA, compose: "new", app: null, view: null, role: null, build: null };
}

/**
 * EVERY "New build" / "New project" DOOR OPENS THE COMPOSER (operator, 2026-10-07), never an
 * agent chat: the top bar, the Factory's header and empty states, the palette. "Plan it with an
 * agent" is an option inside the composer. Pushes the URL and announces it, as a rail click does.
 */
export function openNewProject(): void {
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(newProjectPatch())) {
      if (v === null) url.searchParams.delete(k); else url.searchParams.set(k, v);
    }
    if (url.pathname !== "/") {          // a route page (Identity, My account): the console is at /
      window.location.assign(`/?${url.searchParams}`);
      return;
    }
    window.history.pushState(null, "", `/?${url.searchParams}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}
