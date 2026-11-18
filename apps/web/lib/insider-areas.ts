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

/** The rule, from one read of the self-fix list. */
export function insiderFrom(read: Awaited<ReturnType<typeof readSelfFix<ListView>>> | null): InsiderState {
  if (!read) return null;
  if (!read.ok) return read.status >= 400 && read.status < 500 ? false : null;
  if (read.notModified) return null;
  const list = read.data as Partial<ListView> | null;
  // Not the list's shape (an older API, a proxy's empty answer): not known, so nothing is hidden.
  const n = typeof list?.count === "number" ? list.count : Array.isArray(list?.self_fixes) ? list.self_fixes.length : null;
  return n === null ? null : n > 0;
}

/** Area ids that only show on an insider install. */
export const INSIDER_AREAS = new Set<string>(["self_fixes"]);

/** Whether an insider item is shown, given what the read said. */
export const insiderShown = (state: InsiderState): boolean => state !== false;

export function useInsider(projectId: string | null | undefined): InsiderState {
  const [state, setState] = useState<InsiderState>(null);
  useEffect(() => {
    if (!projectId) return undefined;
    const ctl = new AbortController();
    void readSelfFix<ListView>("list", { project_id: projectId }, { signal: ctl.signal })
      .then((r) => { if (!ctl.signal.aborted) setState(insiderFrom(r)); })
      .catch(() => undefined);
    return () => ctl.abort();
  }, [projectId]);
  return state;
}
