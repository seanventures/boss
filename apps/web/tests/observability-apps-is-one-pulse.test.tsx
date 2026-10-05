// @vitest-environment happy-dom
/**
 * Observability › Apps is ONE Pulse (rewrites monitoring-is-one-panel and
 * boss-is-reachable-from-monitoring for the new area, spec §4.9, §9.2).
 *
 * The old screen had a health panel and a stack panel listing the same apps; the Pulse is one row
 * per app (state, strip, uptime, p95 or how long it has been down), ordered by the server, and
 * an app the stack scrapes but nothing health-checks is NAMED under NOT WATCHED, never dropped.
 * The workflow "Set up monitoring" link (`watch=<app>|<env>`) lands on that app's drawer. Ask
 * BOSS keeps the old starters, built from the apps on screen, the sick one asked about AS its
 * deployment; `Open BOSS here` opens the dock with no question.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { emptyPulseWire, NOW_MS } from "./_helpers/obs-fixtures";
import { $, $$, click, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("one Pulse", () => {
  it("one row per app, in the server's order, each saying its state in a word", async () => {
    await openObs("?view=apps");
    const rows = $$("[data-testid='obs-pulse-row']");
    expect(rows.map((r) => r.getAttribute("data-target"))).toEqual(["t_down", "t_slow", "t_quiet", "t_live", "t_cloud"]);
    expect(rows.map((r) => text(r.querySelector(".inf-state-word")))).toEqual(["Down", "Slow", "No signal", "Up", "Not deployed"]);
    const live = rows[3];
    expect(text(live.querySelector("[data-testid='obs-pulse-uptime']"))).toBe("99.9%");
    expect(text(live.querySelector("[data-testid='obs-pulse-p95']"))).toBe("p95 212 ms");
    expect(text(rows[4].querySelector(".obs-nostrip"))).toBe("Not deployed");
  });

  it("a Down row says how long, from when it went down", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW_MS });
    await openObs("?view=apps");
    expect(text($("[data-testid='obs-pulse-row'][data-target='t_down'] [data-testid='obs-pulse-p95']"))).toBe("down 1h 4m");
  });

  it("the header counts what the Pulse shows", async () => {
    await openObs("?view=apps");
    const counts = text($("[data-testid='obs-head-counts']"));
    expect(counts).toContain("1 up");
    expect(counts).toContain("1 slow");
    expect(counts).toContain("1 down");
    expect(counts).toContain("$3.12 est. · 24h");
  });

  it("an app scraped but not health-checked is named, not dropped", async () => {
    await openObs("?view=apps");
    const nw = $("[data-testid='obs-not-watched']")!;
    expect(text(nw)).toContain("orders · dev");
    expect(nw.querySelector("a[href='http://grafana.local/d/orders']")).not.toBeNull();
  });

  it("nothing deployed: one line and the way to Infra", async () => {
    await openObs("?view=apps", { pulse: emptyPulseWire() });
    expect(text($("[data-testid='obs-apps-empty']"))).toBe("Nothing deployed yet Infra ↗");
    expect($("[data-testid='obs-pulse-row']")).toBeNull();
  });

  it("an unreachable API is one state, never a false empty Pulse", async () => {
    // Every Observability read fails (the API is down). The header says Can't reach BOSS; the
    // tab must not also claim "Nothing deployed yet" or "Every scraped app is watched".
    const down = vi.fn(async () => new Response(JSON.stringify({ detail: "Postgres is not reachable." }),
                                                 { status: 503, headers: { "content-type": "application/json" } }));
    const { mount, settle } = await import("./_helpers/live-render");
    const { ObsHome } = await import("../app/observability/obs-home");
    const React = await import("react");
    vi.stubGlobal("fetch", down);
    window.history.replaceState(null, "", "/?area=observability&view=apps");
    await mount(React.createElement(ObsHome, { projectId: "p1", actor: "ceo" }));
    await settle();
    expect(text($("[data-testid='obs-partial-unreachable']"))).toBe("Can't reach BOSS");
    expect($("[data-testid='obs-apps-empty']")).toBeNull();
    expect(document.body.textContent).not.toContain("Every scraped app is watched");
    expect(document.body.textContent).not.toContain("Nothing deployed yet");
  });

  it("a pin is a link to that deploy in Infra", async () => {
    await openObs("?view=apps");
    const pin = $("[data-testid='obs-pulse-row'][data-target='t_live'] svg.obs-pins a");
    expect(pin?.getAttribute("href")).toBe("?area=infra&view=deploys&deploy=stack%3Asd_live");
    expect(pin?.getAttribute("aria-label")).toMatch(/^Deployed a1b2c3d · \d\d:\d\d · Live$/);
  });

  it("the workflow's watch= link opens that app's drawer", async () => {
    await openObs("?view=apps&watch=invoice%7Cprod");
    expect(params().get("app")).toBe("t_live");
    expect(params().get("watch")).toBeNull();
    expect($("[data-testid='obs-app-drawer']")).not.toBeNull();
  });

  it("an unknown watch= becomes Find with the app's name", async () => {
    await openObs("?view=apps&watch=ghost%7Cprod");
    expect(params().get("app")).toBeNull();
    expect(params().get("q")).toBe("ghost");
  });
});

describe("Ask BOSS is reachable from Observability", () => {
  it("offers the old starters, the sick app asked about as its deployment", async () => {
    await openObs("?view=apps");
    await click($("[data-testid='obs-ask']"));
    const starters = $$("[data-testid='obs-ask-starter']");
    expect(starters.length).toBeGreaterThanOrEqual(2);
    // The starter IS the question the dock is prefilled with: shown whole, not as a tooltip.
    expect(starters[0].textContent).toContain("api, team-inbox are not healthy");
    expect(starters.some((s) => (s.textContent ?? "").includes("I want to change what we monitor"))).toBe(true);
    await click(starters[0]);
    expect(params().get("dock_ask")).toContain("api, team-inbox");
  });

  it("one sick app is asked about AS that deployment", async () => {
    const { pulseWire, PULSE_ROWS } = await import("./_helpers/obs-fixtures");
    await openObs("?view=apps", { pulse: pulseWire({ rows: PULSE_ROWS.filter((r) => r.target_id !== "t_slow") }) });
    await click($("[data-testid='obs-ask']"));
    await click($$("[data-testid='obs-ask-starter']")[0]);
    expect(params().get("agent")).toBe("deployment:t_down");
    expect(params().get("dock_ask")).toContain("Why is api (prod) down");
  });

  it("Open BOSS here opens the dock with no question", async () => {
    await openObs("?view=apps");
    await click($("[data-testid='obs-ask']"));
    await click($("[data-testid='obs-ask-open']"));
    expect(params().get("agent")).toBe("general");
    expect(params().get("dock_ask")).toBeNull();
  });
});
