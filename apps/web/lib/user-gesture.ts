/**
 * WHETHER THE PERSON JUST ASKED FOR SOMETHING — the one test the step-up sheet is opened behind.
 *
 * "Confirm it's you" opened on a plain refresh of the Command Center and the Factory (operator
 * report, 2026-10-10, ~10 times). Nobody had pressed anything: a panel's effect sent a write on
 * load — Repo and models' automatic re-check of stale rows (POST /api/setup op=checks) — the API
 * answered 401 step_up_required because the second factor was over ten minutes old, and the one
 * write path (lib/api-write.ts) opened the sheet for it. Every loaded area stays mounted, so the
 * Setup board's effect ran on every page view of every area once Setup had been opened.
 *
 * The rule is now central instead of per call site: the sheet opens only for a write the person
 * started — a trusted press on a control (a button, a form's submit), not a rail
 * or link navigation — within the last GESTURE_WINDOW_MS. A write nobody started (an effect, a
 * timer, a background sync) is refused quietly and its caller shows its own inline state; it is
 * never allowed to put a modal in front of someone reading a page.
 *
 * Callers that KNOW they are background say so too (`noStepUp` / `background`), so the window
 * cannot be borrowed by an automatic write that happens to follow a press.
 */

/** Long enough for a press whose write waits on a slow first round trip; short enough that a
 *  timer firing later is not mistaken for the press. */
export const GESTURE_WINDOW_MS = 15_000;

/** Controls whose press is a request to act. */
const ACTION_CONTROL = "button, [role=button], input[type=submit], input[type=button], "
  + "input[type=checkbox], input[type=radio], [data-interactive]";
/** Presses that move around rather than act: the rail, links, tabs. Opening an area is a view. */
const NAVIGATION = "nav, [data-area-nav], a[href], [role=tab], [data-navigation]";

let lastGestureAt = -Infinity;
let installed = false;

function now(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

/** Whether `target` is a control whose press asks for an action (and not a navigation). */
export function isActionTarget(target: EventTarget | null): boolean {
  const el = target as (Element & { closest?: (s: string) => Element | null }) | null;
  if (!el || typeof el.closest !== "function") return false;
  if (el.closest(NAVIGATION)) return false;
  return Boolean(el.closest(ACTION_CONTROL));
}

/** Form controls whose CHANGE is a request to act: picking a model in a menu saves it.
 *  boss-1, 2026-11-03: Pin a job › Build saved on a <select> change, the second factor was older
 *  than ten minutes, and the write was refused as "nobody started it" — no "Confirm it's you",
 *  only a red toast, on every try. A select's choice never passes through pointerdown/click on
 *  an ACTION_CONTROL (the native list is outside the page), so the change itself is the press. */
const FORM_CONTROL = "select, input, textarea, [contenteditable=true]";

/** Record a trusted press. Exported for tests; the listeners below call it in the browser. */
export function noteGesture(event: { target: EventTarget | null; type: string; key?: string;
                                     isTrusted?: boolean }): void {
  if (event.isTrusted === false) return;  // a script's dispatchEvent is never the person
  if (event.type === "keydown" && event.key !== "Enter" && event.key !== " ") return;
  if (event.type === "change") {
    const el = event.target as (Element & { closest?: (s: string) => Element | null }) | null;
    if (el && typeof el.closest === "function" && el.closest(FORM_CONTROL)
        && !el.closest(NAVIGATION)) lastGestureAt = now();
    return;
  }
  // A form's submit is a request to act wherever it came from (Enter in a field included).
  if (event.type !== "submit" && !isActionTarget(event.target)) return;
  lastGestureAt = now();
}

/** Install the capture-phase listeners once per page. Safe to call repeatedly and on the server. */
export function installGestureTracking(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  const opts = { capture: true, passive: true } as const;
  window.addEventListener("pointerdown", noteGesture, opts);
  window.addEventListener("click", noteGesture, opts);
  window.addEventListener("keydown", noteGesture as (e: KeyboardEvent) => void, opts);
  window.addEventListener("submit", noteGesture, opts);
  window.addEventListener("change", noteGesture, opts);
}

/** Whether a write starting now was started by the person. */
export function isInteractive(): boolean {
  return now() - lastGestureAt <= GESTURE_WINDOW_MS;
}

/** Tests: forget every press. */
export function resetGesturesForTest(): void {
  lastGestureAt = -Infinity;
}
