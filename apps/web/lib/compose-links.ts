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

/** ADDING A FEATURE IS PROJECT › NEW WORK (Add a project v2, c-after-create.html note 3; the
 *  main use case): the door opens the project's Work tab with New work focused (`compose=1`).
 *  Workflows › New build is gone (BOSSAPP-989): every old door lands here or on Add a project. */
export function addFeaturePatch(appKey?: string | null): { area: "project" } & Record<string, string | null> {
  return { area: "project", tab: "work", compose: "1", app: appKey ?? null, view: null, role: null, build: null };
}

/** Every "New build" door's tooltip. Each door is a GatedButton on workflow_run:start, what the
 *  composer's Start sends (POST /v1/delivery-workflows/start). */
export const NEW_BUILD_TIP = "Add a project: start a new app, or bring existing code";

/** A NEW APP opens Add a project (one screen, app/start-project/add-project.tsx) wherever the
 *  person is: the sheet is drawn by the mounted StartProjectHost (`?new_project=idea`). */
export function newProjectPatch(door: "idea" | "1" = "idea"): Record<string, string | null> {
  return { new_project: door, compose: null, app: null };
}

/**
 * EVERY "New build" / "New project" DOOR OPENS ADD A PROJECT (Add a project v2 absorbed Workflows
 * › New build's new app), never an agent chat: the top bar, the Factory's header and empty
 * states, the palette. "Plan with an agent first" is an option inside it. Pushes the URL and
 * announces it, as a rail click does.
 */
export function openNewProject(door?: "idea" | "1" | unknown): void {
  // Also an onClick handler as is (its event arrives here): anything but "1" (the two doors) is a new app.
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(newProjectPatch(door === "1" ? "1" : "idea"))) {
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
