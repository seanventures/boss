/**
 * "This area is on screen with its data" — the signal the shell's loading bar ends on
 * (independent test D11). An area that can tell dispatches it once per mount; the shell keeps
 * its own heuristic (document done + DOM quiet) for areas that cannot.
 */
export const PAINTED_EVENT = "boss:area-painted";

/** Say it once, after the browser has painted the frame (two animation frames). */
export function announcePainted(area: string): void {
  if (typeof window === "undefined") return;
  window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
    window.dispatchEvent(new CustomEvent(PAINTED_EVENT, { detail: { area } }));
  }));
}
