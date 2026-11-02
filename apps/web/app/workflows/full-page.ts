"use client";

/**
 * FULL PAGE: one workflow, or one run, with the whole window (operator, 2026-11-01: "for big
 * workflows like this full view will be helpful… I can't really view the whole thing").
 *
 *   ?area=delivery_workflows&wf=<key>&page=full     the workflow: pipeline, visual, JSON, versions
 *   ?area=delivery_workflows&wfr=<run>&page=full    the run: its lane left to right, the step below
 *
 * It is the same screen as the inline one, not a second copy: the console's sidebar, top bar and
 * project strip step aside (ConsoleShell reads `useFullPage`), and the area draws the editor, or
 * the run, at the full width and height. Leaving drops `page=` and keeps the rest, so Back to
 * library / Back to runs land where the inline view would.
 */
import { useEffect, useState } from "react";

export const PAGE_PARAM = "page";
export const FULL_PAGE = "full";

/** What a URL shows full page, or null when it shows the console as usual. Only this area's
 *  URLs, and only when they name something to show: `page=full` alone is not a page. */
export function readFullPage(search: string): { workflow: string | null; run: string | null } | null {
  const q = new URLSearchParams(search);
  if (q.get("area") !== "delivery_workflows" || q.get(PAGE_PARAM) !== FULL_PAGE) return null;
  const workflow = q.get("wf")?.trim() || null;
  const run = q.get("wfr")?.trim() || null;
  return workflow || run ? { workflow, run } : null;
}

/** The link to one workflow's, or one run's, full page, keeping the org and project the console
 *  is on. A workflow wins over a run: the editor replaces the runs when both are named. */
export function fullPageHref(search: string, target: { wf: string } | { wfr: string }): string {
  const q = new URLSearchParams(search);
  for (const k of ["tab", "step", "view", "v", "from", "wf", "wfr"]) q.delete(k);
  q.set("area", "delivery_workflows");
  if ("wf" in target) q.set("wf", target.wf);
  else { q.set("wfr", target.wfr); q.set("tab", "runs"); }
  q.set(PAGE_PARAM, FULL_PAGE);
  return `?${q}`;
}

/** Whether the console is showing a full page now. Read after mount (the URL is the client's)
 *  and on every popstate, which is how this area announces its own URL changes. */
export function useFullPage(): boolean {
  const [full, setFull] = useState(false);
  useEffect(() => {
    const read = () => setFull(readFullPage(window.location.search) !== null);
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  return full;
}
