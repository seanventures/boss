"use client";

import { useEffect, useRef } from "react";

import {
  consoleEventsUrl,
  createEventSubscription,
  type RefreshCoordinator,
} from "../lib/console-refresh";

/**
 * The shell subscribes to the realtime event stream (BOSSAPP-835) and turns a matching event into
 * ONE coalesced refresh of what is on screen (lib/console-refresh.ts createEventSubscription).
 * Polling is untouched: this only makes a change show sooner. No EventSource (an old browser, a
 * test) or a stream that never connects changes nothing.
 */
export function useConsoleEvents(coordinator: RefreshCoordinator, projectId: string | null,
                                 activeArea: string): void {
  const area = useRef(activeArea);
  area.current = activeArea;
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.EventSource !== "function") return;
    const subscription = createEventSubscription({
      open: () => new window.EventSource(consoleEventsUrl(projectId)) as unknown as
        ReturnType<Parameters<typeof createEventSubscription>[0]["open"]>,
      watching: () => [area.current],
      // A hidden tab asks for nothing (the poll it replaces would not have run either).
      invalidate: () => {
        if (document.visibilityState === "hidden") return;
        coordinator.request("poll");
      },
    });
    return () => subscription.close();
  }, [coordinator, projectId]);
}
