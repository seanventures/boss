// @vitest-environment happy-dom
/**
 * The pack says why there is no dashboard, beside "No dashboard link yet",
 * not only on that span's title. A blank or whitespace why says nothing.
 * More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the why stays padded, a blank why still renders, a long why is a
 * span, the "No dashboard link yet" words disappear, or the pack stops calling
 * dashboardWhySentence(pack.dashboardWhy).
 */
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MonitoringPackCard, type MonitoringPack } from "../app/workflows/monitoring-pack";
import { $, mount, unmount } from "./_helpers/live-render";
import { codeOnly } from "./_helpers/source";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const screen = await import("../app/workflows/monitoring-pack");
const dashboardWhySentence = (screen as {
  dashboardWhySentence?: (why: string | null | undefined) => string | null;
}).dashboardWhySentence;

const LONG = "Grafana has no folder for this app yet";
const PADDED = `  ${LONG}  `;
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "no folder yet";

const WHO: MonitoringPack["who"] = {
  kind: "agent", roleKey: "sre", role: "Site Reliability Engineer",
  name: null, how: "named", skills: [], onBehalfOf: null, model: null,
};

function pack(dashboardWhy: string | null, dashboardUrl: string | null = null): MonitoringPack {
  return {
    app: "Order API", environment: "staging", summary: "", adding: false, branch: "",
    manifestPath: "boss.deploy.yaml", rules: [], runbooks: [], panels: [], tuned: [],
    gate: "block", rulesHref: null, dashboardUrl, dashboardWhy, missing: false, why: null, who: WHO,
  };
}

afterEach(() => { unmount(); });

describe("dashboardWhySentence", () => {
  it("is the trimmed why, or nothing when the why is blank", () => {
    expect(dashboardWhySentence!(PADDED)).toBe(LONG);
    expect(dashboardWhySentence!(SHORT)).toBe(SHORT);
    expect(dashboardWhySentence!(SIX)).toBe(SIX);
    expect(dashboardWhySentence!(`  ${SEVEN}  `)).toBe(SEVEN);
    expect(dashboardWhySentence!("")).toBeNull();
    expect(dashboardWhySentence!("   ")).toBeNull();
    expect(dashboardWhySentence!(" \n\t ")).toBeNull();
    expect(dashboardWhySentence!(null)).toBeNull();
    expect(dashboardWhySentence!(undefined)).toBeNull();
  });
});

describe("the pack says why there is no dashboard", () => {
  it("keeps No dashboard link yet and shows a long why as a paragraph beside it", async () => {
    const shown = pack(PADDED);
    await mount(<MonitoringPackCard pack={shown} />);
    const label = $("[data-testid='mp-dashboard-why']");
    expect(label?.textContent).toBe("No dashboard link yet");
    expect(label?.getAttribute("title")).toBe(shown.dashboardWhy);
    expect(label?.querySelector("[data-testid='mp-dashboard-why-said']")).toBeNull();
    const said = $("[data-testid='mp-dashboard-why-said']");
    expect(said).not.toBeNull();
    expect(said?.tagName).toBe("P");
    expect(said?.textContent).toBe(LONG);
    expect(said?.hasAttribute("data-user-text")).toBe(true);
    expect(label?.nextElementSibling).toBe(said);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
  });

  it("shows six words as a span and seven as a paragraph", async () => {
    const sixPack = pack(`  ${SIX}  `);
    await mount(<MonitoringPackCard pack={sixPack} />);
    const sixLabel = $("[data-testid='mp-dashboard-why']");
    expect(sixLabel?.textContent).toBe("No dashboard link yet");
    expect(sixLabel?.getAttribute("title")).toBe(sixPack.dashboardWhy);
    const sixSaid = $("[data-testid='mp-dashboard-why-said']");
    expect(sixSaid?.tagName).toBe("SPAN");
    expect(sixSaid?.textContent).toBe(SIX);
    expect(sixSaid?.hasAttribute("data-user-text")).toBe(true);
    expect(sixLabel?.nextElementSibling).toBe(sixSaid);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    unmount();

    const sevenPack = pack(SEVEN);
    await mount(<MonitoringPackCard pack={sevenPack} />);
    const sevenLabel = $("[data-testid='mp-dashboard-why']");
    expect(sevenLabel?.textContent).toBe("No dashboard link yet");
    expect(sevenLabel?.getAttribute("title")).toBe(SEVEN);
    const sevenSaid = $("[data-testid='mp-dashboard-why-said']");
    expect(sevenSaid?.tagName).toBe("P");
    expect(sevenSaid?.textContent).toBe(SEVEN);
    expect(sevenSaid?.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    unmount();

    const shortPack = pack(`  ${SHORT}  `);
    await mount(<MonitoringPackCard pack={shortPack} />);
    const shortLabel = $("[data-testid='mp-dashboard-why']");
    expect(shortLabel?.textContent).toBe("No dashboard link yet");
    expect(shortLabel?.getAttribute("title")).toBe(shortPack.dashboardWhy);
    const shortSaid = $("[data-testid='mp-dashboard-why-said']");
    expect(shortSaid?.tagName).toBe("SPAN");
    expect(shortSaid?.textContent).toBe(SHORT);
    expect(shortSaid?.hasAttribute("data-user-text")).toBe(true);
  });

  it("shows no sentence when the why is blank and keeps the span title", async () => {
    const blank = pack(" \n\t ");
    await mount(<MonitoringPackCard pack={blank} />);
    const label = $("[data-testid='mp-dashboard-why']");
    expect(label?.textContent).toBe("No dashboard link yet");
    expect(label?.getAttribute("title")).toBe(" \n\t ");
    expect($("[data-testid='mp-dashboard-why-said']")).toBeNull();

    unmount();

    await mount(<MonitoringPackCard pack={pack(null)} />);
    const empty = $("[data-testid='mp-dashboard-why']");
    expect(empty?.textContent).toBe("No dashboard link yet");
    expect(empty?.hasAttribute("title")).toBe(false);
    expect($("[data-testid='mp-dashboard-why-said']")).toBeNull();
  });

  it("keeps the dashboard link and does not say there is no dashboard", async () => {
    await mount(<MonitoringPackCard pack={pack(PADDED, "https://grafana.example.test/d/boss-order-api")} />);
    expect($("[data-testid='mp-dashboard-link']")?.getAttribute("href")).toBe("https://grafana.example.test/d/boss-order-api");
    expect($("[data-testid='mp-dashboard-link']")?.textContent).toBe("Dashboard ↗");
    expect($("[data-testid='mp-dashboard-why']")).toBeNull();
    expect($("[data-testid='mp-dashboard-why-said']")).toBeNull();
  });

  it("calls dashboardWhySentence with pack.dashboardWhy and keeps the span", () => {
    const src = codeOnly(join(__dirname, "../app/workflows/monitoring-pack.tsx"));
    const start = src.indexOf("pack.dashboardUrl");
    expect(start).toBeGreaterThan(-1);
    const branch = src.slice(start, src.indexOf("pack.missing", start) === -1 ? src.length : src.length);
    expect(branch).toContain("dashboardWhySentence(pack.dashboardWhy)");
    expect(branch).toContain('data-testid="mp-dashboard-why"');
    expect(branch).toContain("No dashboard link yet");
    expect(branch).toContain("title={pack.dashboardWhy ?? undefined}");
    expect(branch).toContain('data-testid="mp-dashboard-why-said"');
    expect(src).toContain("dashboardWhySentence(pack.dashboardWhy)");
  });
});
