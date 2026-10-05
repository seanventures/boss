/**
 * When the Talk to BOSS dock opens beside a window too narrow for both, the sidebar steps down
 * to its icon rail (the operator's own collapse, not a new state) so the page keeps a usable
 * column. Pure, so the rule is tested without a browser; ConsoleShell applies it.
 */

/** The page column below which the sidebar folds to icons while the dock is open. */
export const MIN_PAGE_COLUMN = 720;
/** The full sidebar, as design-system.css draws it. */
export const SIDEBAR_FULL = 244;

/** True when the dock is open and, with the full sidebar, the page would be under 720px. */
export function railForDock({ dockOpen, viewport, dockWidth, sidebar = SIDEBAR_FULL }: {
  dockOpen: boolean; viewport: number; dockWidth: number; sidebar?: number;
}): boolean {
  if (!dockOpen || viewport <= 0) return false;
  // The dock sits over the page, which pads itself by the dock's width + 16 (ConsoleShell).
  return viewport - sidebar - (dockWidth + 16) < MIN_PAGE_COLUMN;
}

/** The icon rail's width (globals.css `.bx-shell.nav-docked`). */
export const SIDEBAR_RAIL = 84;
/** The page column the dock never squeezes below, with the sidebar folded. */
export const MIN_PAGE_WITH_DOCK = 560;
export const DOCK_MIN = 320;

/** The dock's width: the operator's dragged width when there is one, else clamp(360px, 40vw,
 *  520px); either way capped so the page keeps MIN_PAGE_WITH_DOCK beside the folded sidebar
 *  (the page pads itself by the dock's width + 16), and never under DOCK_MIN. */
export function dockWidthFor(viewport: number, dragged?: number | null): number {
  const wanted = dragged && dragged >= DOCK_MIN ? dragged : Math.min(520, Math.max(360, Math.round(viewport * 0.4)));
  const cap = viewport - SIDEBAR_RAIL - 16 - MIN_PAGE_WITH_DOCK;
  return Math.max(DOCK_MIN, Math.min(wanted, cap));
}
