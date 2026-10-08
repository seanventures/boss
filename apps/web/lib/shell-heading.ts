/**
 * WHAT THE TOP BAR SAYS, AND WHICH RAIL ROW IS LIT, ON EVERY PAGE.
 *
 * The area page (/) shows one area at a time: the one in `?area=`, else the last one opened
 * (localStorage "boss.area"). A ROUTE page (/identity/**, /account/**) is a different page with
 * its own address — the route says what it is, and nothing remembered from the area page may
 * override it. It did: Identity › Groups opened with "Command Center" in the top bar and two lit
 * rail rows, because the remembered area won over the route.
 */

export type HeadingArea = { id: string; label: string; sub: string };

/** The area a page shows. On a route page: the route's own area (or none, for My account). */
export function shownArea(opts: {
  routeMode: boolean;
  /** The route's area (Identity → "enterprise"); null for a route with no rail row. */
  routeArea: string | null;
  /** `?area=` on the area page. */
  requested: string | null;
  /** The last area opened on the area page. */
  stored: string | null;
  known: readonly string[];
  fallback: string;
}): string | null {
  const ok = (id: string | null): id is string => !!id && opts.known.includes(id);
  if (opts.routeMode) return ok(opts.routeArea) ? opts.routeArea : null;
  if (ok(opts.requested)) return opts.requested;
  if (ok(opts.stored)) return opts.stored;
  return opts.fallback;
}

/** The top bar's title and line under it. A route page with its own title (My account) uses it;
 *  a route page in an area (Identity) names that area; the area page names the area it shows. */
export function shellHeading(
  areas: readonly HeadingArea[],
  opts: { routeMode: boolean; routeArea: string | null; active: string; title?: { label: string; sub: string } },
): { label: string; sub: string } {
  if (opts.title) return opts.title;
  const id = opts.routeMode ? opts.routeArea : opts.active;
  const area = areas.find((a) => a.id === id) ?? (opts.routeMode ? null : areas[0]);
  return area ? { label: area.label, sub: area.sub } : { label: "", sub: "" };
}
