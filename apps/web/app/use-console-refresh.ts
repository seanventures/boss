"use client";

import { useCallback } from "react";
import { useRouter } from "next/navigation";

import { requestConsoleRefresh, type RefreshKind } from "../lib/console-refresh";

/**
 * How a panel asks for fresh server data. Never `router.refresh()` directly: refreshes asked
 * for faster than they complete starve every commit (lib/console-refresh.ts).
 *
 * `"poll"` for a timer, `"now"` for something the operator just did.
 */
export function useConsoleRefresh(): (kind?: RefreshKind) => void {
  const router = useRouter();
  return useCallback((kind: RefreshKind = "poll") => requestConsoleRefresh(router, kind), [router]);
}
