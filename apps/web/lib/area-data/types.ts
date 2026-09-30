/**
 * BOSS-13: shared types for per-area console data loaders.
 *
 * Area loaders isolate the monolithic page.tsx fetch fan-out so each console
 * area can load only what it needs. When `active` is false the loader returns
 * empty stubs without hitting the API.
 */

export type AreaLoadOptions = {
  /** When false, return empty stubs and skip network calls. */
  active?: boolean;
  projectId: string;
};

export function isAreaActive(
  requestedArea: string | undefined,
  areaIds: readonly string[],
): boolean {
  // No deep-link / overview: still load shared areas so client-side tab switch
  // from Command Center has data. Deep-links to an unrelated area skip the pack.
  if (!requestedArea || requestedArea === "overview") {
    return true;
  }
  return areaIds.includes(requestedArea);
}
