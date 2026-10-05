/**
 * Govern's fixtures: GET /v1/govern as the API answers it, and a fake `/api/govern` that records
 * every request (method, op, body, If-Match) the screen makes.
 */
import { vi } from "vitest";

type Json = Record<string, unknown>;

export function control(key: string, over: Json = {}): Json {
  return {
    key, name: key, section: "agents", value_effective: 1, value_stored: null, default: null, source: "default",
    unit: "count", bounds: { min: 1, max: 10, step: 1 }, editable_by: "self",
    write: { method: "PUT", path: `/v1/settings/${key}` }, enforced: { word: "Each agent", effect_key: "each_agent" },
    evidence: null, advisory: false, note: null, rule: null, version: "unset", ...over,
  };
}

export function governView(over: Json = {}): Json {
  return {
    project_id: "p1",
    meter: {
      period: "day", state: "green", state_label: "Under cap", cap_usd_micros: 10_000_000,
      actual_usd_micros: 3_120_000, reserved_usd_micros: 400_000, committed_usd_micros: 3_520_000,
      available_usd_micros: 6_480_000, utilization_ratio: 0.352, active_reservations_count: 2,
      burn_rate_usd_micros_per_hour: 310_000, burn_window_hours: 24, forecasted_24h_usd_micros: 7_800_000,
      time_to_cap_seconds: 79_200, resets_at: "2026-10-05T00:00:00+00:00", resets_at_label: "00:00 UTC",
      included_in_subscription_usd_micros: 12_300_000, soft_cap_pct: 0.7, auto_lite_mode_pct: 0.85,
      freeze_provisioning_pct: 0.9, freeze_tier2_pct: 0.95, tier2_frozen: false, top_cost_drivers: [],
      periods: [
        { period: "day", cap_usd_micros: 10_000_000, actual_usd_micros: 3_120_000, reserved_usd_micros: 400_000,
          committed_usd_micros: 3_520_000, utilization_ratio: 0.352, budget_state: "green",
          resets_at: "", resets_at_label: "00:00 UTC", state_label: "Under cap" },
        { period: "month", cap_usd_micros: 200_000_000, actual_usd_micros: 41_800_000, reserved_usd_micros: 0,
          committed_usd_micros: 41_800_000, utilization_ratio: 0.209, budget_state: "green",
          resets_at: "", resets_at_label: "00:00 UTC on 1 November", state_label: "Under cap" },
      ],
    },
    controls: [
      control("daily_cap", { name: "Daily cap", section: "spend", unit: "usd_micros", value_effective: 10_000_000,
                             editable_by: "pace", write: null, enforced: { word: "Hard cap", effect_key: "hard_cap" },
                             evidence: { word: "Stopped 2× · 7d", count_7d: 2, log: { kind: "budget.reservation_denied" } },
                             version: "7" }),
      control("monthly_cap", { name: "Monthly cap", section: "spend", unit: "usd_micros", value_effective: 200_000_000,
                               source: "set", write: { method: "PUT", path: "/v1/setup/budget" },
                               enforced: { word: "Hard cap", effect_key: "hard_cap" },
                               evidence: { word: "Never stopped", count_7d: 0, log: { kind: "budget.reservation_denied" } },
                               version: "7" }),
      control("burst", { name: "Burst", section: "spend", unit: "usd_micros_per_min", value_effective: 100_000,
                         editable_by: "pace", write: null, enforced: { word: "Waits", effect_key: "waits" },
                         evidence: { word: "Held 4× · 7d", count_7d: 4, log: { kind: "budget.refused" } }, version: "7" }),
      control("pace", { name: "Pace", section: "spend", unit: "intensity", value_effective: null, bounds: { min: 0, max: 100, step: 1 },
                        write: { method: "POST", path: "/v1/budget/set_intensity" },
                        enforced: { word: "Preset", effect_key: "preset" }, version: "7" }),
      control("code_agent_max_concurrency", { name: "Review lanes", value_effective: 6, value_stored: 3, source: "set",
                                              bounds: { min: 1, max: 16, step: 1 },
                                              enforced: { word: "Review panel", effect_key: "review_panel" },
                                              note: { kind: "floor", stored: 3, floor: 6 },
                                              rule: { kind: "floor", floor: 6, fallback: 3 }, version: "2026-10-04T10:00:00+00:00" }),
      control("code_agent_max_turns", { name: "Turn budget", value_effective: 90, default: 90, bounds: { min: 10, max: 200, step: 1 },
                                        note: { kind: "default" },
                                        evidence: { word: "Hit 1× · 7d", count_7d: 1, log: { kind: "code_task.implement_continued" } } }),
      control("code_review_inrun_max_rounds", { name: "Review rounds", value_effective: 2, value_stored: 0, source: "set",
                                                bounds: { min: 0, max: 5, step: 1 },
                                                enforced: { word: "Review loop", effect_key: "review_loop" },
                                                note: { kind: "reads_as", stored: 0, reads_as: 2 },
                                                rule: { kind: "zero_reads_as", value: 2 }, version: "2026-10-04T09:00:00+00:00" }),
      control("captain_leads_projects", { name: "Captain leads", unit: "switch", value_effective: 0,
                                          bounds: { min: 0, max: 1, step: 1 },
                                          enforced: { word: "Every build", effect_key: "every_build" } }),
      control("max_concurrent_jobs", { name: "Jobs at once", value_effective: 3, editable_by: "pace", write: null, bounds: null,
                                       enforced: { word: "Job queue", effect_key: "job_queue" } }),
      control("max_workers_total", { name: "Workers", value_effective: 2, editable_by: "pace", write: null, bounds: null,
                                     enforced: { word: "Scaling", effect_key: "scaling" } }),
    ],
    thresholds: [
      { key: "soft_cap_pct", label: "Heads-up", pct: 0.7, passed: false },
      { key: "auto_lite_mode_pct", label: "Cheaper builds", pct: 0.85, passed: false },
      { key: "freeze_provisioning_pct", label: "No new servers", pct: 0.9, passed: false },
      { key: "freeze_tier2_pct", label: "Risky work stops", pct: 0.95, passed: false },
    ],
    token_guides: {
      advisory: true, enforced: { word: "Advisory", effect_key: "advisory" }, schema_ready: true,
      default: { stage: "*", max_total_tokens_per_call: null, max_tokens_per_day: 5_000_000,
                 max_cost_usd_micros_per_day: 50_000_000, alert_at_pct: 0.8, source: "default" },
      per_step: [{ stage: "implement", max_total_tokens_per_call: 96_000, max_tokens_per_day: null,
                   max_cost_usd_micros_per_day: null, alert_at_pct: 0.8, source: "default" },
                 { stage: "repair", max_total_tokens_per_call: 96_000, max_tokens_per_day: null,
                   max_cost_usd_micros_per_day: null, alert_at_pct: 0.8, source: "stored", updated_at: "2026-10-04T08:00:00+00:00" }],
    },
    pace: { value: null, envelope: null },
    versions: { budget: "7" },
    ...over,
  };
}

export type GovernCall = { method: string; url: string; op?: string; body?: Json; ifMatch?: string | null };

export type FakeGovern = {
  calls: GovernCall[];
  view: Json;
  spend: Json | null;
  envelopes: Json | null;
  status: number;
  writeStatus: number;
  writeBody: Json;
};

export function newGovern(over: Partial<FakeGovern> = {}): FakeGovern {
  return { calls: [], view: governView(), spend: null, envelopes: null, status: 0, writeStatus: 0,
           writeBody: { ok: true }, ...over };
}

export function fakeGovernApi(g: FakeGovern) {
  return vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = new Headers(init?.headers as HeadersInit | undefined);
    const call: GovernCall = { method, url: `${url.pathname}?${url.searchParams.toString()}` };
    if (method !== "GET" && init?.body) {
      const body = JSON.parse(String(init.body)) as Json;
      call.op = String(body.op ?? "");
      call.body = body;
      call.ifMatch = headers.get("If-Match");
    }
    g.calls.push(call);
    if (url.pathname === "/api/govern" && method === "GET") {
      if (g.status) return Response.json({ error: "connect ECONNREFUSED" }, { status: g.status });
      const view = url.searchParams.get("view");
      if (view === "spend") return Response.json(g.spend ?? {});
      if (view === "envelopes") return Response.json(g.envelopes ?? {});
      return Response.json(g.view, { headers: { ETag: "\"v1\"" } });
    }
    if (url.pathname === "/api/govern" && method === "POST") {
      const op = call.op;
      if (op === "pace_preview") {
        const i = Number(call.body?.intensity ?? 0);
        return Response.json({ envelope: { operational_intensity: i, daily_cap_usd_micros: 252_000_000,
          monthly_cap_usd_micros: 5_040_000_000, burst_cap_usd_micros_per_min: 5_250_000, max_concurrent_jobs: 11,
          max_workers_total: 15 }, preview: { period: "day", cap_usd_micros: 252_000_000, days_considered: 30,
          days_with_spend: 9, days_stopped: 0, stopped_days: [], not_enough_history: false, daily: [] } });
      }
      if (op === "cap_preview") {
        return Response.json({ period: call.body?.period, cap_usd_micros: Number(call.body?.cap_usd) * 1e6, days_considered: 30,
          days_with_spend: 9, days_stopped: 3, stopped_days: ["a", "b", "c"], not_enough_history: false,
          daily: [{ day: "2026-10-01", money_usd_micros: 9_000_000, included_usd_micros: 0 }] });
      }
      if (g.writeStatus) return Response.json(g.writeBody, { status: g.writeStatus });
      return Response.json(g.writeBody);
    }
    return Response.json({});
  });
}
