/**
 * THE ONE UNSAVED-CHANGES GUARD. A form that holds typed work nobody has saved yet must not let
 * it vanish silently: closing or reloading the tab gets the browser's own "Leave site?" prompt
 * (beforeunload), and an in-app move that would drop the draft (another page, Cancel, New) asks
 * first. Written once for the workflow editor; the Wiki's new page lost its text without a word
 * (BOSSAPP-1037), so the guard lives here and both use it.
 */
import { useCallback, useEffect, useRef } from "react";

/** What an in-app move away from unsaved work asks. */
export const LEAVE_UNSAVED = "Leave without saving? What you typed here will be lost.";

/**
 * Holds a beforeunload guard while `dirty`, and returns `leave(go)`: runs `go` at once when
 * nothing is unsaved, otherwise only after the person confirms. Returns whether it went.
 */
export function useUnsavedGuard(dirty: boolean): (go: () => void) => boolean {
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  return useCallback((go: () => void) => {
    if (dirtyRef.current && !window.confirm(LEAVE_UNSAVED)) return false;
    go();
    return true;
  }, []);
}
