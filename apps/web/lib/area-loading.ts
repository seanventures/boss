/**
 * WHICH AREAS THE SERVER RENDERS: the one asked for, plus the ones this session has opened.
 *
 * The trade has swung twice, and both ends were reported as unusable:
 *
 *   * Every area on every load (shows() === true): 8.3 MB and 6-9 s for the first paint, and
 *     every auto-refresh repeated it. An operator's laptop heated up on a video call.
 *   * One area per request: 25-235 KB, but every rail click became a server round trip and a
 *     full React teardown — "it is taking really really horrible time to load".
 *
 * Neither is necessary. The first full load renders ONLY the requested area, so the first
 * paint is one area's worth. Opening another area for the first time adds it to a session
 * cookie and refreshes; the server then renders every area this session has opened, at the
 * same positions in the tree, so the ones already on screen are updated rather than remounted.
 * After that first visit a click is the old pure CSS reveal — no fetch, no mount. The shell
 * shows a real loading state for the first visit (ConsoleShell, `area-loading`).
 *
 * Pure, so the rule is testable without a server.
 */

export const LOADED_AREAS_COOKIE = "boss_areas";

/** Area ids are short snake/kebab words. Anything else in the cookie is ignored. */
const AREA_ID = /^[a-z][a-z0-9_-]{0,39}$/;
/** More than every area there is; a bound so a forged cookie cannot ask for a thousand. */
const MAX_AREAS = 64;

export function parseLoadedAreas(raw: string | null | undefined): string[] {
  if (!raw) return [];
  let decoded = raw;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    /* a malformed escape is just ignored below */
  }
  const out: string[] = [];
  for (const part of decoded.split(/[,\s]+/)) {
    if (AREA_ID.test(part) && !out.includes(part)) out.push(part);
    if (out.length >= MAX_AREAS) break;
  }
  return out;
}

/** The cookie value with `area` added. */
export function cookieWith(raw: string | null | undefined, area: string): string {
  const areas = parseLoadedAreas(raw);
  if (AREA_ID.test(area) && !areas.includes(area)) areas.push(area);
  return areas.slice(0, MAX_AREAS).join(",");
}

/**
 * The areas this render includes.
 *
 * `fullDocument` is a browser load or reload (not a router refresh, a soft navigation or a
 * server action's re-render): it renders the requested area alone, so the first paint is
 * fast however many areas an earlier tab opened. Everything else renders what the session
 * has opened, so a refresh never unmounts an area the operator already has on screen.
 */
export function areasToRender(input: {
  requested: string;
  cookie: string | null | undefined;
  fullDocument: boolean;
}): Set<string> {
  const requested = AREA_ID.test(input.requested) ? input.requested : "overview";
  if (input.fullDocument) return new Set([requested]);
  return new Set([requested, ...parseLoadedAreas(input.cookie)]);
}

/**
 * Whether a request is a full document load, from its headers. A router refresh or soft
 * navigation carries `RSC: 1`; a server action carries `Next-Action`. Both re-render the page
 * the operator already has open, so both must keep its areas.
 */
export function isFullDocumentRequest(header: (name: string) => string | null): boolean {
  return !header("rsc") && !header("next-action") && !header("next-router-state-tree");
}

/** Read the cookie value in the browser. */
export function loadedAreasCookieValue(cookieString: string): string {
  for (const part of cookieString.split(/;\s*/)) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq) === LOADED_AREAS_COOKIE) return part.slice(eq + 1);
  }
  return "";
}

/** The Set-Cookie string the shell writes: session-scoped, the whole console, never sent cross-site. */
export function loadedAreasCookie(value: string): string {
  return `${LOADED_AREAS_COOKIE}=${encodeURIComponent(value)}; path=/; SameSite=Lax`;
}
