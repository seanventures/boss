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
export const COMPOSE_AREA = "workflows";

export function addFeaturePatch(appKey?: string | null): Record<string, string | null> {
  return { area: COMPOSE_AREA, compose: "add", app: appKey ?? null, view: null, role: null, build: null };
}
