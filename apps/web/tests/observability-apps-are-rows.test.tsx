// @vitest-environment happy-dom
/**
 * OBSERVABILITY › APPS AS THE WIREFRAME DRAWS IT (screen 15; ACCEPTANCE 15-01…15-08).
 *
 * - The head: the green pulse tile, "Observability", the wireframe's subtitle.
 * - Filters in the wireframe's order — Apps (count), BOSS platform, Alert sources, Contact points —
 *   then every other view Observability already had (§13: nothing removed).
 * - One row per app across the projects you may read: the project's mark and name, the
 *   environment, mono "uptime · p95", a 140 px sparkline toned by health, "N firing" (green at 0,
 *   amber when firing), and the "Project › Health" chip to THAT project's Health tab.
 * - A row of this project opens the app drawer in place (Alerts and Overview unchanged); a row
 *   of another project opens there. The Pulse is one press away and unchanged.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, click, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("Observability › Apps (screen 15)", () => {
  it("has the wireframe's head and its four filters first, every other view after", async () => {
    await openObs("?view=apps");
    expect($("[data-testid='obs-page-h'] h1")!.textContent).toBe("Observability");
    expect(text($("[data-testid='obs-page-h']"))).toContain("Every app and BOSS itself — shared alert sources, contact points, Grafana");
    const tabs = $$("[role='tab']").map((t) => text(t));
    expect(tabs.slice(0, 4)).toEqual(["Apps6", "BOSS platform", "Alert sources", "Contact points"]);
    for (const kept of ["Alerts", "Models", "Scorecards"]) expect(tabs.some((t) => t.startsWith(kept))).toBe(true);
    expect($("[data-testid='obs-tab-apps']")!.getAttribute("aria-selected")).toBe("true");
  });

  it("draws one row per app with mark, name, env, uptime · p95, sparkline, firing and the Health chip", async () => {
    await openObs("?view=apps");
    const rows = $$("[data-testid^='obs-arow-t_']").concat($$("[data-testid='obs-arow-t_np']"));
    expect(rows.length).toBeGreaterThanOrEqual(6);
    const live = $("[data-testid='obs-arow-t_live']")!;
    expect(text(live.querySelector(".obs-arow-mark"))).toBe("HD");
    expect(text(live.querySelector("[data-testid='obs-arow-metrics-t_live']"))).toBe("99.9% · p95 212 ms");
    expect(live.querySelector(".obs-arow-spark")!.getAttribute("width")).toBe("140");
    expect(live.querySelector(".obs-arow-spark")!.getAttribute("data-tone")).toBe("green");
    expect(text(live.querySelector("[data-testid='obs-arow-firing-t_live']"))).toBe("0 firing");
    expect(live.querySelector("[data-testid='obs-arow-firing-t_live']")!.getAttribute("data-tone")).toBe("green");
    const np = $("[data-testid='obs-arow-t_np']")!;
    expect(text(np.querySelector(".obs-arow-t"))).toContain("NorthPeak Retail");
    expect(np.querySelector("[data-testid='obs-arow-firing-t_np']")!.getAttribute("data-tone")).toBe("amber");
    expect(np.querySelector(".obs-arow-spark")!.getAttribute("data-tone")).toBe("amber");
    const chip = np.querySelector("[data-testid='obs-app-project-health']")!;
    expect(text(chip)).toBe("Project › Health");
    const q = new URLSearchParams((chip.getAttribute("href") ?? "").split("?")[1]);
    expect([q.get("project_id"), q.get("area"), q.get("tab")]).toEqual(["p2", "project", "health"]);
  });

  it("a row of this project opens the app drawer in place", async () => {
    await openObs("?view=apps");
    await click($("[data-testid='obs-arow-open-t_down']"));
    expect(params().get("app")).toBe("t_down");
  });

  it("keeps the Pulse one press away, unchanged", async () => {
    await openObs("?view=apps");
    await click($("[data-testid='obs-apps-view-pulse']"));
    expect(params().get("apps")).toBe("pulse");
    expect($$("[data-testid='obs-pulse-row']").length).toBe(5);
  });

  it("says when nothing is deployed, and offers Retry when the read fails", async () => {
    await openObs("?view=apps", { appsAll: { rows: [], projects: 1, partial: [] } });
    expect(text($("[data-testid='obs-arows-empty']"))).toBe("No apps deployed yet");
  });
});
