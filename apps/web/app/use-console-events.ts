"use client";

import { useEffect, useRef } from "react";

import {
  consoleEventsUrl,
  createEventSubscription,
  isStorageEvent,
  type RefreshCoordinator,
} from "../lib/console-refresh";
import { notifyChanged } from "./changed";

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
      // "*": an event every area shows (the Infra Agent's storage strip, BOSSAPP-1156).
      watching: () => [area.current, "*"],
      // A hidden tab asks for nothing (the poll it replaces would not have run either).
      invalidate: (reason) => {
        if (document.visibilityState === "hidden") return;
        coordinator.request("poll");
        // The strip, the critical card and the Storage card are client polls (usePolled), which
        // restart on the console's change signal; a storage event is that signal.
        if (reason.event && isStorageEvent(reason.event.event_type)) notifyChanged("events", { event_type: reason.event.event_type });
      },
    });
    return () => subscription.close();
  }, [coordinator, projectId]);
}
