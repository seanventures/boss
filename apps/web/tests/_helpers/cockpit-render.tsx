/**
 * Mount the cockpit against a fixture: `fetch` is stubbed by URL, so the real poll, parsers and
 * components run. Posts are recorded for the action tests.
 */
import { vi } from "vitest";

type W = Record<string, unknown>;
export type Route = (url: string, init?: RequestInit) => unknown | Response | undefined;

export const posts: { url: string; body: W }[] = [];

/** `cockpit` is the GET /v1/code-tasks/{id}/cockpit body (a fixture), or a function of the url. */
export function stubFetch(cockpit: W | ((url: string) => W | Response), extra: Route = () => undefined): void {
  posts.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") {
      let body: W = {};
      try { body = JSON.parse(String(init.body ?? "{}")) as W; } catch { body = {}; }
      posts.push({ url, body });
    }
    const custom = extra(url, init);
    if (custom instanceof Response) return custom;
    if (custom !== undefined) return Response.json(custom);
    if (url.includes("/cockpit")) {
      const got = typeof cockpit === "function" ? cockpit(url) : cockpit;
      return got instanceof Response ? got : Response.json(got, { headers: { etag: String((got as W).etag ?? "") } });
    }
    if (url.includes("/tools")) return Response.json({ items: [{ tool_name: "Edit", ok: true, latency_ms: 340 }, { tool_name: "Bash", ok: false, latency_ms: 1200 }], more: false });
    if (url.includes("/api/run-acts")) {
      return Response.json({ act_id: "act_BUILD0000000001", role_key: "software_engineer", kind: "stage", status: "active",
        root_act_id: "act_ROOT", handed_by_kind: "boss", how: "next_stage", edge_label: "Next stage", label: "Implement · Add CSV export",
        accountable: "ceo", started_at: "2026-10-04T09:18:00.000Z", links: {}, artifacts: [], considered: [], scope: {} });
    }
    if (url.includes("/api/activity")) return Response.json({ items: [{ id: "l1", created_at: "2026-10-04T09:12:00.000Z", title: "Cloned the repo", agent: "", level: "info", body: "" }] });
    if (url.includes("/api/delivery-workflows")) return Response.json({ items: [] });
    if (init?.method === "POST") return Response.json({ status: "ok" });
    return Response.json({});
  }));
}

export function stubMatchMedia(trueQueries: string[]): void {
  const mm = (query: string) => ({
    matches: trueQueries.some((q) => query.includes(q)), media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
    dispatchEvent: () => false,
  });
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: mm });
}

/** Visible text runs (an element's own text), outside tooltips, text boxes and user text. */
export function visibleRuns(root: Element): string[] {
  const SKIP = "[data-tip], [role='tooltip'], [title], textarea, script, style, option, [hidden], "
    + "[aria-hidden='true'], .ag-sr, [data-user-text]";
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("").trim();
    if (own) out.push(own);
  }
  return out;
}
