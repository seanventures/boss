"use client";

import { useCallback, useRef } from "react";
import { useRouter } from "next/navigation";

import { requestConsoleRefresh, type RefreshKind } from "../lib/console-refresh";
import { useOnScreen } from "../lib/area-shown";

/**
 * How a panel asks for fresh server data. Never `router.refresh()` directly: refreshes asked
 * for faster than they complete starve every commit (lib/console-refresh.ts).
 *
 * `"poll"` for a timer, `"now"` for something the operator just did.
 *
 * A TIMER IN AN AREA NOBODY IS LOOKING AT ASKS FOR NOTHING. A full refresh re-renders every area
 * the session has opened; the Discovery list asked for one every 4 s while any dossier was
 * researching, from a hidden area, for as long as the console stayed open (lib/area-shown.ts).
 */
export function useConsoleRefresh(): (kind?: RefreshKind) => void {
  const router = useRouter();
  const onScreen = useOnScreen();
  const onScreenRef = useRef(onScreen);
  onScreenRef.current = onScreen;
  return useCallback((kind: RefreshKind = "poll") => {
    if (kind === "poll" && !onScreenRef.current) return;
    requestConsoleRefresh(router, kind);
  }, [router]);
}
