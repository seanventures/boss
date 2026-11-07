// @vitest-environment happy-dom
/**
 * ONE ALERTS LIST, TWO SCOPES (operator, 2026-11-07: "what is Alerts under Observability vs Alerts
 * under Health in each project — are they same or we have some differences?").
 *
 * Observability › Alerts is every app of every project you may read plus BOSS's own platform
 * (GET /v1/alerts/all): its first line says "All apps · 6 · and BOSS itself", each row names its
 * project (a link to that project's Health), BOSS's own rows say BOSS, and Ack goes to each row's
 * own project. A project's Health draws the same component over its slice (see
 * px-health-reads-like-the-wireframe). An incident that recovered but is still open says why and
 * when BOSS closes it on its own — never a bare "the incident is still open".
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { FIRING_ALERT, allAlertsWire } from "./_helpers/alerts-fixtures";
import { $, $$, click, settle, unmount } from "./_helpers/live-render";
import { openObs, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const tag = (pid: string, name: string) => ({ project_id: pid, project_name: name, project_href: `?project_id=${pid}&area=project&tab=health` });
const MINE = { ...FIRING_ALERT, id: "alrt_mine", ...tag("p1", "HelpDesk") };
const THEIRS = { ...FIRING_ALERT, id: "alrt_theirs", target_id: "t_np", app: "checkout", incident: null, ...tag("p2", "NorthPeak") };
const BOSS = { ...FIRING_ALERT, id: "alrt_boss", target_id: "svc_boss", app: "BOSS", env: "platform", alertname: "BossEvaluatorLag",
               label: "Evaluator lag", incident: null, platform: true, ...tag("p1", "HelpDesk") };
const HELD = {
  id: "inc_held", title: "helpdesk-ai-v3 (staging): p95 latency 940.9 ms", status: "diagnosing", sev: "sev3", lifecycle: "mitigating",
  since: new Date(Date.now() - 3 * 3600_000).toISOString(), firing: 0, happening: false, subject_id: "t_down", urgency: "high",
  acked: false, response_run_id: null, response_status: null,
  word: "Recovered; too little traffic to confirm it; it closes on its own unless it fires again",
  status_chip: { key: "recovered", tone: "green", word: "Recovered", tip: "Nothing fires now" },
  recovered_at: new Date(Date.now() - 20 * 60_000).toISOString(),
  open_reason: { key: "quiet", who: "boss", closes_at: new Date(Date.now() + 10 * 60_000).toISOString() },
  ...tag("p1", "HelpDesk"),
};
const every = () => allAlertsWire([MINE, THEIRS, BOSS], {
  project_id: null, counts: { firing: 3, pending: 0, needs_attention: 3 },
  scope: { kind: "all", apps: 6, projects: 2, boss: true },
  apps: [{ target_id: "t_down", app: "api", env: "prod", ...tag("p1", "HelpDesk") },
         { target_id: "t_np", app: "checkout", env: "prod", ...tag("p2", "NorthPeak") }],
  incidents: [HELD],
});

describe("Observability › Alerts covers every app and BOSS itself, and says so", () => {
  it("leads with its scope and links to this project's slice in Health", async () => {
    const rec = await openObs("?view=alerts", { alerts: every });
    expect(rec.calls.some((u) => u.startsWith("/v1/alerts/all?"))).toBe(true);
    expect($("[data-testid='obs-alerts']")?.getAttribute("data-scope")).toBe("all");
    expect(text($("[data-testid='obs-alerts-scope']"))).toContain("All apps · 6 · and BOSS itself");
    const other = $("[data-testid='obs-alerts-other']")!;
    expect(text(other)).toContain("Only HelpDesk › Health");
    expect(other.getAttribute("href")).toContain("area=project");
    expect(other.getAttribute("href")).toContain("tab=health");
  });

  it("names each row's project, and BOSS's own rows as BOSS", async () => {
    await openObs("?view=alerts", { alerts: every });
    const chip = (id: string) => $(`[data-testid='obs-alert-${id}'] [data-testid='obs-alert-project']`);
    expect(text(chip("alrt_mine"))).toBe("HelpDesk");
    expect(text(chip("alrt_theirs"))).toBe("NorthPeak");
    expect(chip("alrt_theirs")?.getAttribute("href")).toContain("project_id=p2");
    expect(text(chip("alrt_boss"))).toBe("BOSS");
    const opts = $$("[data-testid='obs-alerts-app'] option").map((o) => text(o));
    expect(opts).toContain("checkout · prod · NorthPeak");
  });

  it("acks each row in its own project", async () => {
    const rec = await openObs("?view=alerts", { alerts: every, writes: { "/alerts/ack": { body: { acked: ["x"] } } } });
    for (const box of $$("[data-testid='obs-alert-select']")) await click(box);
    await click($("[data-testid='obs-alerts-ack-selected']"));
    await settle();
    const acks = rec.writes.filter((w) => w.url === "/v1/alerts/ack").map((w) => w.body as { project_id: string; alert_ids: string[] });
    const byProject = Object.fromEntries(acks.map((a) => [a.project_id, [...a.alert_ids].sort()]));
    expect(byProject).toEqual({ p1: ["alrt_boss", "alrt_mine"], p2: ["alrt_theirs"] });
  });

  it("a recovered incident that is still open says why and when BOSS closes it", async () => {
    await openObs("?view=alerts", { alerts: every });
    const row = $("[data-testid='obs-incident-inc_held']")!;
    expect(row.getAttribute("data-why")).toBe("quiet");
    const word = text(row.querySelector("[data-testid='obs-incident-word']"));
    expect(word).toMatch(/^Recovered at \d\d:\d\d; too little traffic to confirm it; it closes on its own unless it fires again — by \d\d:\d\d$/);
    expect(word).not.toContain("the incident is still open");
    expect(row.querySelector("[data-testid='obs-incident-word']")?.getAttribute("title")).toMatch(/Closes on its own at/);
  });
});
