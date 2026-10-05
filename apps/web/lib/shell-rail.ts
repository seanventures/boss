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
