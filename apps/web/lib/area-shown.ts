/**
 * WHETHER THE AREA THIS PANEL LIVES IN IS ON SCREEN (app/area-frame.tsx provides it).
 *
 * Every area the session opened stays mounted and the CSS reveal only hides it, so a panel's
 * timers kept running in areas nobody was looking at — 126 browser requests a minute on a
 * console sitting on the Command Center, ~80% from hidden areas (FINDINGS, 2026-10-10). Every
 * poll in the console asks this before it fetches: the shared polling hooks (usePolled,
 * usePolledRead, useLogStream, useLiveRefresh) and each panel's own interval. Outside an area
 * (the header, a route page, a unit test) the answer is `true`, as it always was.
 */
import { createContext, useContext, useEffect, useState } from "react";

export const AreaShownContext = createContext<boolean>(true);

/** True while this panel's area is the one on screen. */
export function useAreaShown(): boolean {
  return useContext(AreaShownContext);
}

/** True while the tab is visible AND this panel's area is on screen: the one test a poll asks. */
export function useOnScreen(): boolean {
  const shown = useAreaShown();
  const [tabVisible, setTabVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState !== "hidden");
  useEffect(() => {
    const on = () => setTabVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return shown && tabVisible;
}
