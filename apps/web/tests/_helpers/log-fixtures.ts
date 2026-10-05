/**
 * The Activity log's fixtures: API rows as /v1/activity returns them, and a fake API at the
 * console's route handlers (`/api/activity…`) that records every request it is asked.
 */
import { vi } from "vitest";

export type ApiRow = Record<string, unknown>;

export function row(id: string, over: ApiRow = {}): ApiRow {
  return {
    id, source: "event", kind: "workflow.step_ready", status: "info", level: "info",
    title: `Line ${id}`, actor: "boss", entity_type: "workflow_run", entity_id: "wfr_1",
    stage: "", run_id: "", workflow_run_id: "", agent: "", service: "", body: "",
    created_at: new Date(Date.parse("2026-10-04T10:00:00Z") - Number(id.replace(/\D/g, "") || 0) * 1000).toISOString(),
    data: {}, ...over,
  };
}

export function rows(n: number, over: (i: number) => ApiRow = () => ({})): ApiRow[] {
  return Array.from({ length: n }, (_, i) => row(`r${i}`, {
    created_at: new Date(Date.parse("2026-10-04T10:00:00Z") - i * 30_000).toISOString(), ...over(i) }));
}

export type FakeLog = {
  asked: string[];
  page: { items: ApiRow[]; next_cursor?: string | null; newest_cursor?: string | null; labels?: Record<string, string>; gap?: boolean };
  tail: ApiRow[];
  histogram: Record<string, unknown> | null;
  facets: Record<string, unknown> | null;
  status: number;
  error: string;
  tailEtag: string;
};

export function histogramOf(counts: number[], seconds = 900, start = "2026-10-04T00:00:00Z"): Record<string, unknown> {
  const buckets = counts.map((n, i) => ({
    t: new Date(Date.parse(start) + i * seconds * 1000).toISOString(),
    error: i === 2 ? 1 : 0, warn: 0, info: n, verbose: 0,
  }));
  const total = buckets.reduce((s, b) => s + b.error + b.info, 0);
  return { buckets, bucket_seconds: seconds, from: start, to: start, total, total_is_exact: true };
}

export const FACETS = {
  levels: { error: 12, warn: 88, info: 4100, verbose: 900 },
  sources: { event: 3000, llm_call: 400, agent_turn: 1200, deployment: 3 },
  statuses: { failed: 12, ok: 300, info: 3000 },
  services: { api: 20, worker: 40 },
  agents: [{ role_key: "software_engineer", n: 300 }, { role_key: "adversarial_reviewer", n: 120 }],
  runs: [{ id: "wfr_1", label: "Lead Triage Inbox", n: 50 }, { id: "crun_7", label: "Ship it #182", n: 40 }],
  total: 5100, exact: true,
};

/** A fake BOSS behind the console's route handlers. Pass `log` to steer it between steps. */
export function fakeLogApi(log: FakeLog) {
  return vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    log.asked.push(`${url.pathname}?${url.searchParams.toString()}`);
    if (log.status) return Response.json({ error: log.error || "down" }, { status: log.status });
    const etag = (init?.headers as Record<string, string> | undefined)?.["If-None-Match"];
    if (url.pathname === "/api/activity/histogram") return Response.json(log.histogram ?? histogramOf([1, 2, 3]));
    if (url.pathname === "/api/activity/facets") return Response.json(log.facets ?? FACETS);
    if (url.pathname.startsWith("/api/activity/row/")) {
      const id = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      const found = [...log.page.items, ...log.tail].find((r) => r.id === id);
      return found ? Response.json({ item: found, labels: {} }) : Response.json({ item: null, gone: true, labels: {} }); // as the route answers (D14b)
    }
    if (url.pathname === "/api/activity/context") {
      return Response.json({ anchor: url.searchParams.get("id"), items: log.page.items.slice(0, 5), labels: {} });
    }
    if (url.pathname === "/api/activity") {
      if (url.searchParams.get("after")) {
        if (etag && etag === log.tailEtag && !log.tail.length) return new Response(null, { status: 304, headers: { ETag: log.tailEtag } });
        const fresh = log.tail.splice(0);
        return Response.json({ items: fresh, next_cursor: null, labels: {}, gap: false },
                             { headers: { ETag: log.tailEtag } });
      }
      return Response.json({ ...log.page, labels: log.page.labels ?? {} }, { headers: { ETag: "\"page\"" } });
    }
    if (url.pathname === "/api/run-acts" || url.pathname.startsWith("/api/agent")) return Response.json({});
    return Response.json({});
  });
}

export function newLog(over: Partial<FakeLog> = {}): FakeLog {
  return { asked: [], page: { items: [], next_cursor: null, newest_cursor: null }, tail: [], histogram: null,
           facets: null, status: 0, error: "", tailEtag: "\"tail\"", ...over };
}
