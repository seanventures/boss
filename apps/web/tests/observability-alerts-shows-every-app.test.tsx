// @vitest-environment happy-dom
/**
 * Observability › Alerts (BOSSAPP-598): every app's firing and recent alerts, filtered by app,
 * state and severity in the URL; an app's name opens its drawer on the Alerts tab (the one place
 * its rules change). And the header's Grafana ↗ shows only once BOSS has checked that Grafana
 * answers; otherwise it says so, with Check.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { FIRING_ALERT, NOISE_ALERT, allAlertsWire } from "./_helpers/alerts-fixtures";
import { GRAFANA_DOWN, pulseWire } from "./_helpers/obs-fixtures";
import { $, $$, click, settle, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

async function choose(sel: HTMLSelectElement | null, value: string) {
  sel!.value = value;
  sel!.dispatchEvent(new Event("change", { bubbles: true }));
  await settle();
}

describe("Observability › Alerts", () => {
  it("is a tab with the firing count, and lists every app's alerts", async () => {
    const rec = await openObs("?view=alerts");
    expect(text($("[data-testid='obs-tab-alerts']"))).toContain("Alerts");
    expect(text($("[data-testid='obs-tab-alerts'] .inf-tab-count"))).toBe("2");
    const rows = $$("[data-testid^='obs-alert-alrt_']");
    expect(rows).toHaveLength(2);
    expect(text(rows[0].querySelector("[data-testid='obs-alert-app']"))).toBe("api");
    expect(text(rows[0])).toContain("Agent: Opened an incident");
    expect(text($("[data-testid='obs-alerts-count']"))).toBe("2 firing");
    expect(rec.calls.some((u) => u.startsWith("/v1/alerts?project_id=p1&state=firing"))).toBe(true);
  });

  it("filters by app, state and severity, in the URL", async () => {
    const rec = await openObs("?view=alerts", { alerts: (u) => allAlertsWire(u.searchParams.get("state") === "all" ? [FIRING_ALERT, NOISE_ALERT] : [FIRING_ALERT]) });
    await choose($("[data-testid='obs-alerts-app']") as HTMLSelectElement, "t_down");
    await choose($("[data-testid='obs-alerts-state']") as HTMLSelectElement, "all");
    await choose($("[data-testid='obs-alerts-severity']") as HTMLSelectElement, "sev1");
    expect(params().get("aapp")).toBe("t_down");
    expect(params().get("astate")).toBe("all");
    expect(params().get("asev")).toBe("sev1");
    await settle();
    expect(rec.calls.some((u) => u.includes("/v1/alerts?project_id=p1&app=t_down&state=all&severity=sev1&since=7d"))).toBe(true);
  });

  it("an app's name opens its drawer on the Alerts tab", async () => {
    await openObs("?view=alerts");
    await click($("[data-testid='obs-alert-alrt_err'] [data-testid='obs-alert-app']"));
    await settle();
    expect(params().get("app")).toBe("t_down");
    expect(params().get("atab")).toBe("alerts");
    expect($("[data-testid='obs-app-alerts']")).not.toBeNull();
  });

  it("nothing firing is said, not drawn as an empty table", async () => {
    await openObs("?view=alerts", { alerts: () => allAlertsWire([], { counts: { firing: 0, pending: 0 } }) });
    expect(text($("[data-testid='obs-alerts-empty']"))).toBe("Nothing firing");
  });
});

describe("the header's Grafana ↗", () => {
  it("links once Grafana answers", async () => {
    await openObs("?view=apps");
    expect($("[data-testid='obs-grafana']")?.getAttribute("href")).toBe("http://grafana.local");
  });

  it("otherwise says so, and Check asks it now", async () => {
    const rec = await openObs("?view=apps", {
      pulse: pulseWire({ grafana: GRAFANA_DOWN }),
      writes: { "/grafana/check": { body: { grafana: { state: "not_answering", reason: "Grafana isn't answering on its host" } } } },
    });
    const off = $("[data-testid='obs-grafana']")!;
    expect(off.tagName).toBe("SPAN");
    expect(text($("[data-testid='obs-grafana-word']"))).toBe("Grafana not answering");
    expect($("[data-testid='obs-grafana-word']")?.getAttribute("title")).toBe("Grafana isn't answering on its host (connection refused)");
    await click($("[data-testid='obs-grafana-check']"));
    await settle();
    expect(rec.writes.map((w) => w.url)).toContain("/v1/observability/grafana/check");
  });

  it("never links to an unchecked Grafana", async () => {
    await openObs("?view=apps", { pulse: pulseWire({ grafana: { state: "unchecked", url: "http://grafana.local", reason: "BOSS hasn't checked Grafana yet" } }) });
    expect($("[data-testid='obs-grafana']")?.tagName).toBe("SPAN");
    expect(text($("[data-testid='obs-grafana-word']"))).toBe("Grafana not checked");
  });
});
