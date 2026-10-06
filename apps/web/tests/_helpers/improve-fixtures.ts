/**
 * A fake /api/improve for the Improve BOSS / scorecards / feedback screens. Records every call;
 * answers each view with realistic data (docs/product/RUN_SCORECARDS.md, "Implementation").
 */
import { vi } from "vitest";

type Json = Record<string, unknown>;
export type ImproveCall = { method: string; view?: string | null; op?: string; body?: Json; url: string };

const sw = (on: boolean, asked: boolean, label: string, def: boolean) =>
  ({ on, by: asked ? "ceo" : null, at: asked ? new Date(Date.now() - 3600_000).toISOString() : null, label, default: def, asked });

export function newImprove() {
  const weights = [
    ["tests", "Tests pass", 0.5], ["gates", "Gates clear", 0.2], ["review", "Review approved", 0.2],
    ["intact", "Old tests intact", 0.1], ["approvals", "Approvals", 0.2], ["prd_approved", "PRD approved", 0.6],
    ["panel_agreed", "Panel agreed", 0.4], ["completed_unverified", "Done, no evidence", 0.6],
    ["merged_bonus", "Merged bonus", 0.15], ["revert_penalty", "Reverted", 0.4], ["rollback_penalty", "Rolled back", 0.3],
    ["unhealthy_24h_penalty", "Unhealthy at 24h", 0.2], ["cost_penalty", "Cost over median", 0.04],
    ["turns_penalty", "Turns over median", 0.03], ["steers_penalty", "Steers over median", 0.03],
  ].map(([key, label, value]) => ({ key, label, value, default: value }));
  const payload = {
    v: 1, day: "2026-10-06", install_id: "bi_abcdefghijklmnopqrstuvwxyz", secret_hash: "a".repeat(64),
    heartbeat: { install_id: "bi_abcdefghijklmnopqrstuvwxyz", version: "1.0.0-rc.4", edition: "community",
                 install_method: "compose", os: "linux", arch: "arm64", age: "8-30", worked_24h: true },
  };
  return {
    calls: [] as ImproveCall[],
    status: 0,
    sharing: {
      switches: { improve: sw(false, false, "Improve BOSS", false), heartbeat: sw(true, false, "Daily heartbeat", true),
                  collector: sw(false, false, "Collector mode", false) },
      env_disabled: null as string | null, install_id: "bi_abcdefghijklmnopqrstuvwxyz",
      url: "https://boss-api.local.sean.network/v1/telemetry/ingest",
      next_due_at: new Date(Date.now() + 8 * 3600_000).toISOString(), last_attempt_at: null,
      sends: [
        { id: "tms_1", day: "2026-10-05", kind: "daily", status: "sent", bytes_raw: 812, bytes_gz: 431, batch_days: [],
          aggregated: {}, http_status: 200, error: null, cut_at: "2026-10-05T03:00:00Z", sent_at: "2026-10-05T03:00:01Z" },
        { id: "tms_2", day: "2026-10-04", kind: "daily", status: "failed", bytes_raw: 812, bytes_gz: 431, batch_days: [],
          aggregated: {}, http_status: null, error: "timeout", cut_at: "2026-10-04T05:00:00Z", sent_at: "2026-10-04T05:00:05Z" },
      ],
      unsent_batches: 0, score: { version: "1", weights }, heartbeat_fields: Object.keys(payload.heartbeat),
    },
    preview: { pending: { id: "tms_3", day: "2026-10-06", cut_at: new Date(Date.now() - 180_000).toISOString(), bytes_raw: 812,
                          bytes_gz: 431, aggregated: {}, batch_days: [], sha256: "f".repeat(64), json: payload },
               why_not: null, next_due_at: new Date(Date.now() + 8 * 3600_000).toISOString() } as Json,
    scorecards: {
      group: "role", win: "30d", kind: "build", score_version: "1", weights,
      total: { n: 12, score: 0.71, cost_usd: 31.2, done: 0.83, merged: 0.6, backfilled: 4 },
      rows: [
        { key: "software_engineer", n: 9, score: 0.78, sd: 0.12, done: 0.89, merged: 0.67, cost_usd: 22.1, turns: 41, steers: 0.3, rounds: 1.2 },
        { key: "backend_engineer", n: 3, score: 0.5, sd: 0.3, done: 0.67, merged: 0.5, cost_usd: 9.1, turns: 60, steers: 1, rounds: 2 },
      ],
      costing: [
        { unit_kind: "build", unit_id: "crun_9", why: "low", title: "Lead triage inbox", score: 0, steers: 2, rounds: 3,
          cost_usd: 4.2, role_key: "backend_engineer", state: "failed", error_class: "tests_failed", ended_at: null,
          href: "/?area=code_tasks&build=crun_9" },
        { unit_kind: "build", unit_id: "crun_7", why: "steers", title: "Billing export", score: 0.6, steers: 3, rounds: 1,
          cost_usd: 2.1, role_key: "software_engineer", state: "completed", error_class: null, ended_at: null,
          href: "/?area=code_tasks&build=crun_7" },
      ],
    } as Json,
    experiments: {
      items: [{
        id: "exp_1", name: "software_engineer: claude_code vs codex", target_kind: "role", target_key: "software_engineer",
        knob: "harness", knob_label: "Harness", arm_a: "claude_code", arm_b: "codex", split_b: 0.5, target_units: 20,
        arm_cost_cap_usd: 25, status: "running", stop_reason: null, capped_arms: [], promoted_arm: null,
        arms: { a: { n: 6, mean: 0.72, sd: 0.1, cost_usd: 8.2, gate_fail: 0.1, assigned: 7 },
                b: { n: 5, mean: 0.81, sd: 0.08, cost_usd: 6.1, gate_fail: 0, assigned: 6 } },
        compare: { verdict: "too_close", diff: 0.09, low: -0.02, high: 0.2, label: "Too close to call", why: "95% interval of B − A" },
      }],
      knobs: [{ key: "harness", label: "Harness", kind: "harness", choices: ["claude_code", "codex"], min: null, max: null },
              { key: "review_rounds", label: "Review rounds", kind: "int", choices: null, min: 0, max: 5 }],
    } as Json,
    prompts: { ask_open: true, asks_left_today: 3, checkin_due: true, mine: {} as Json,
               chips: { build: ["wrong_scope", "poor_quality", "broke_something", "too_slow", "too_expensive", "misread_brief"],
                        research: ["shallow", "wrong_market", "great_sources", "changed_my_mind"],
                        panel: ["missed_requirements", "over_scoped", "good_debate"], answer: [], deploy: [] },
               ratings: { build: ["yes", "with_changes", "no"], research: ["useful", "not_useful"], panel: ["right", "wrong"],
                          answer: ["up", "down"], deploy: ["working", "not_working"] } } as Json,
    collector: {
      proof: "312 active installs this month, 41% still active after 30 days",
      active: { daily: 120, weekly: 240, monthly: 312 }, total_installs: 540, kept_30d_pct: 41,
      new_per_week: [{ week: "2026-09-07", n: 20 }, { week: "2026-09-14", n: 34 }, { week: "2026-09-21", n: 41 }],
      cohorts: [{ week: "2026-09-07", size: 20, pct: [100, 70, 55, 50, 0, 0, 0, 0] }],
      versions: [{ key: "1.0.0-rc.4", n: 250 }, { key: "1.0.0-rc.3", n: 62 }], editions: [{ key: "community", n: 300 }],
      os: [{ key: "linux", n: 290 }], arch: [{ key: "arm64", n: 180 }, { key: "amd64", n: 132 }],
      methods: [{ key: "compose", n: 280 }], opted_in: 61,
      scale: { builds: { at_least: 480, dist: [{ key: "6-20", n: 30 }] }, projects: { at_least: 90, dist: [{ key: "1", n: 40 }] } },
      ship: { rated: 200, yes: 120, with_changes: 50, no: 30, rate: 0.6 },
      chips: [{ key: "too_slow", n: 22 }, { key: "misread_brief", n: 12 }],
      funnel: [{ key: "setup", n: 61 }, { key: "first_build", n: 52 }, { key: "first_pr", n: 40 }, { key: "first_merge", n: 25 },
               { key: "first_deploy", n: 12 }],
      checkin: [{ key: "very", n: 9 }, { key: "somewhat", n: 4 }, { key: "not", n: 1 }],
      by: { harness: [{ key: "claude_code", n: 300, score: 0.74, merged: 0.62 }], model: [{ key: "claude-sonnet-5", n: 280, score: 0.75, merged: 0.6 }],
            role_key: [{ key: "software_engineer", n: 290, score: 0.73, merged: 0.61 }] },
      errors: [{ key: "tests_failed", n: 40 }], installer: null,
    } as Json,
    bundle: { bundle: "build failed at validating\nerror: tests failed [SECRET]", masked: { SECRET: 1 }, chars: 50 } as Json,
  };
}
export type FakeImprove = ReturnType<typeof newImprove>;

export const IMPROVE_PATH = "/api/improve";

/** Answers /api/improve; anything else goes to `rest` (another fake), or 404. */
export function fakeImproveApi(f: FakeImprove, rest?: (input: unknown, init?: RequestInit) => Promise<Response>) {
  return vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    if (url.pathname !== IMPROVE_PATH) return rest ? rest(input, init) : new Response("{}", { status: 404 });
    const method = (init?.method ?? "GET").toUpperCase();
    const call: ImproveCall = { method, url: `${url.pathname}?${url.searchParams.toString()}`, view: url.searchParams.get("view") };
    if (method !== "GET" && init?.body) {
      const body = JSON.parse(String(init.body)) as Json;
      call.op = String(body.op ?? "");
      call.body = body;
    }
    f.calls.push(call);
    if (f.status) return Response.json({ error: "connect ECONNREFUSED" }, { status: f.status });
    if (method === "GET") {
      const v = call.view ?? "";
      const map: Record<string, unknown> = { sharing: f.sharing, preview: f.preview, scorecards: f.scorecards,
        experiments: f.experiments, prompts: f.prompts, collector: f.collector, share_bundle: f.bundle,
        send: { id: url.searchParams.get("id"), json: (f.preview.pending as Json | null)?.json ?? {} } };
      return Response.json(map[v] ?? {});
    }
    if (call.op === "switch") {
      const name = String(call.body?.switch) as "improve" | "heartbeat" | "collector";
      f.sharing.switches[name] = { ...f.sharing.switches[name], on: call.body?.on === true, asked: true, by: "ceo",
                                    at: new Date().toISOString() };
      return Response.json(f.sharing);
    }
    if (call.op === "forget") return Response.json({ deleted: true, status: 200, error: null });
    if (call.op === "feedback") return Response.json({ id: "pfb_1", rating: call.body?.rating, chips: call.body?.chips ?? [] });
    return Response.json({ ok: true });
  });
}
