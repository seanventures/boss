/**
 * YOUR APP (engine services/project_experience/your_app.py): the project's app, always one press
 * away — on the Overview's "Your app" card, the projects list card and Command Center's project
 * rows. Which version it shows (main after a merge, else the latest ready change) and whether its
 * preview runs. A preview BOSS stopped is `asleep`: Open app wakes it.
 */

export type YourAppState = "running" | "starting" | "asleep" | "stopped" | "failed" | "none";

export type YourApp = {
  run_id: string; name: string; title: string;
  version: "main" | "change"; version_label: string; version_words: string;
  pr_number: number | null; pr_url: string;
  preview_id: string; state: YourAppState; words: string;
  /** The preview's console link (running, starting or asleep); "" when there is none. */
  href: string; error: string;
  /** The deployed app (staging/prod), when there is one: Try it stays beside Open app. */
  live_url: string;
};

const STATES: YourAppState[] = ["running", "starting", "asleep", "stopped", "failed", "none"];
type R = Record<string, unknown>;
const o = (v: unknown): R => (v && typeof v === "object" && !Array.isArray(v) ? (v as R) : {});
const s = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");

export function parseYourApp(v: unknown): YourApp | null {
  const x = o(v);
  const run = s(x.run_id);
  if (!run) return null;
  const state = (STATES as string[]).includes(s(x.state)) ? (s(x.state) as YourAppState) : "none";
  const pr = typeof x.pr_number === "number" ? x.pr_number : null;
  return {
    run_id: run, name: s(x.name), title: s(x.title),
    version: s(x.version) === "main" ? "main" : "change",
    version_label: s(x.version_label) || (s(x.version) === "main" ? "Main" : "Latest change"),
    version_words: s(x.version_words),
    pr_number: pr, pr_url: s(x.pr_url),
    preview_id: s(x.preview_id), state, words: s(x.words),
    href: s(x.href), error: s(x.error), live_url: s(x.live_url),
  };
}

/** Open app opens the link at once only when the preview runs (or is already starting); every
 *  other state starts it first through the console's one write path (consent, step-up). */
export function opensDirectly(app: Pick<YourApp, "state" | "href">): boolean {
  return Boolean(app.href) && (app.state === "running" || app.state === "starting");
}

/** Two letters for the app's tile (its thumbnail). */
export function appInitials(name: string): string {
  const words = name.replace(/[^A-Za-z0-9 ]+/g, " ").trim().split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "A") + (words[1]?.[0] ?? words[0]?.[1] ?? "")).toUpperCase();
}
