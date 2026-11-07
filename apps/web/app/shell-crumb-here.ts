/**
 * AN ORG-WIDE PLACE THE BREADCRUMB CANNOT READ FROM THE URL (14-01): a page or drawer that shows
 * one shared thing — a server in Infra — names its trail here while it is open, and the top bar
 * draws "Infra / Servers / 203.0.113.20" instead of "<project> / Infra". The drawer clears it on
 * close. One window event; the breadcrumb listens.
 */
export type CrumbTrail = { area: string; trail: { label: string; href?: string }[] } | null;

const EVENT = "boss:crumb-here";
let current: CrumbTrail = null;

export function setCrumbTrail(t: CrumbTrail): void {
  current = t;
  try { window.dispatchEvent(new CustomEvent(EVENT)); } catch { /* not a browser */ }
}

export function crumbTrail(): CrumbTrail {
  return current;
}

export function onCrumbTrail(fn: () => void): () => void {
  try { window.addEventListener(EVENT, fn); } catch { return () => undefined; }
  return () => window.removeEventListener(EVENT, fn);
}
