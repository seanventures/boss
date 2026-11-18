"use client";

/**
 * INSIDER RAIL ITEMS ONLY WHERE THEY MEAN SOMETHING (stranger test 2026-11-17: "Landed slices"
 * and "Self-fixes" read as insider words on a fresh install). There is no "BOSS developer
 * install" flag, so the rail asks the data: an install that has fixed BOSS itself (any self-fix
 * on record) is one where BOSS is being developed. Everywhere else both stay off the rail; the
 * palette and `?area=self_fixes` still reach the page.
 *
 *   true   there is self-fix data: show them
 *   false  the read answered and there is none (or this viewer may not read it): hide them
 *   null   not known yet, or the read failed: leave the rail as it was (never hide on a guess)
 */
import { useEffect, useState } from "react";

import { readSelfFix, type ListView } from "./self-fix";

export type InsiderState = boolean | null;

/** The rule, from one read: the install-wide probe (`{exists}`) or the self-fix list. */
export function insiderFrom(read: Awaited<ReturnType<typeof readSelfFix<ListView | { exists?: boolean }>>> | null): InsiderState {
  if (!read) return null;
  if (!read.ok) return read.status >= 400 && read.status < 500 ? false : null;
  if (read.notModified) return null;
  const body = read.data as { exists?: unknown; count?: unknown; self_fixes?: unknown } | null;
  if (typeof body?.exists === "boolean") return body.exists;
  // Not the probe's shape (an older API, a proxy's empty answer): not known, so nothing is hidden.
  const n = typeof body?.count === "number" ? body.count : Array.isArray(body?.self_fixes) ? body.self_fixes.length : null;
  return n === null ? null : n > 0;
}

/** A TRUE ANSWER IS KEPT FOR THE SESSION (review of #200): the items never blink off while the
 *  operator moves between projects, and the probe is not asked again. */
const SEEN_KEY = "boss.insider.seen";
let seenThisSession = false;
function seen(): boolean {
  if (seenThisSession) return true;
  try { return window.sessionStorage.getItem(SEEN_KEY) === "1"; } catch { return false; }
}
function remember(): void {
  seenThisSession = true;
  try { window.sessionStorage.setItem(SEEN_KEY, "1"); } catch { /* no storage: this tab's memory only */ }
}
/** Tests only: forget the session's answer. */
export function forgetInsider(): void {
  seenThisSession = false;
  try { window.sessionStorage.removeItem(SEEN_KEY); } catch { /* nothing kept */ }
}

/** Area ids that only show on an insider install. */
export const INSIDER_AREAS = new Set<string>(["self_fixes"]);

/** Whether an insider item is shown, given what the read said. */
export const insiderShown = (state: InsiderState): boolean => state !== false;

/** ASKED OF THE WHOLE INSTALL (review of #200): the open project's own list said no on most of
 *  an operator's projects. `GET /v1/self-fixes/exists` answers for every project of the org. */
export function useInsider(projectId: string | null | undefined): InsiderState {
  const [state, setState] = useState<InsiderState>(() => (typeof window !== "undefined" && seen() ? true : null));
  useEffect(() => {
    if (!projectId) return undefined;
    if (seen()) { setState(true); return undefined; }
    const ctl = new AbortController();
    void readSelfFix<{ exists?: boolean }>("exists", { project_id: projectId }, { signal: ctl.signal })
      .then((r) => {
        if (ctl.signal.aborted) return;
        const got = insiderFrom(r);
        if (got) remember();
        setState(got);
      })
      .catch(() => undefined);
    return () => ctl.abort();
  }, [projectId]);
  return state;
}
