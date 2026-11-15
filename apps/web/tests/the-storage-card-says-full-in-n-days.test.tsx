// @vitest-environment happy-dom
/**
 * S3 · Storage under Govern › Infrastructure (BOSSAPP-1156): usage by type, the growth trend,
 * "full in N days", the last cleanup, pending approvals; loud at 20 % and 10 %, quiet above.
 */
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StorageCardView, forecastLines, linePath, segments } from "../app/infra/storage-card";
import { demoSummary } from "../lib/infra-storage-demo";
import { codeOnly } from "./_helpers/source";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

const noop = { onHost: () => {}, onClean: () => {}, onRule: () => {}, cleaning: false };

describe("the Storage card", () => {
  it("shows Free now · Growth · Full in · Last cleanup, usage by type and the forecast", async () => {
    const s = demoSummary("resolved", null)!;
    await mount(<StorageCardView s={s} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-tile-free"]')!.textContent).toContain("61 %");
    expect($('[data-testid="sc-tile-growth"]')!.textContent).toContain("+2.1 GB/day");
    expect($('[data-testid="sc-tile-full"]')!.textContent).toContain("84 days");
    expect($('[data-testid="sc-tile-full"]')!.textContent).toContain("> 90 days with the rules");
    expect($('[data-testid="sc-tile-cleanup"]')!.textContent).toContain("203 GB freed");
    expect($('[data-testid="sc-thresholds"]')!.textContent).toContain("Resume at 25 %");
    expect($('[data-testid="sc-last-report"]')!.getAttribute("href")).toContain("incident=si_20261114_laptop");
    // One segment per type, the free part striped; the bar says it in words.
    const segs = $$('[data-testid="sc-bar"] .sc-seg:not(.sc-seg-free)');
    expect(segs).toHaveLength(s.byType.filter((t) => t.key !== "free").length);
    expect($('[data-testid="sc-bar"]')!.getAttribute("aria-label")).toMatch(/Backups 9\.6 GB.*free$/);
    expect($('[data-testid="sc-docker"]')).toBeNull();
    // The trend: used, if nothing changed, with rules; the full line; the thresholds.
    const svg = $('[data-testid="sc-trend"] svg')!;
    expect(svg.querySelector(".sc-used")).not.toBeNull();
    expect(svg.querySelector(".sc-flat")).not.toBeNull();
    expect(svg.querySelector(".sc-rules")).not.toBeNull();
    expect(svg.getAttribute("aria-label")).toContain("full in 84 days");
    expect($('[data-testid="sc-thresholds"]')!.textContent).toContain("Alert at 20 % free");
    expect($('[data-testid="sc-thresholds"]')!.textContent).toContain("Critical at 15 % free");
    expect($('[data-testid="sc-thresholds"]')!.textContent).toContain("Hard floor 10 %");
    expect($$('[data-testid="sc-rule"]')).toHaveLength(8);
    expect($$('[data-testid="sc-host"]')).toHaveLength(3);
    // Quiet above the alert: no notice.
    expect($('[data-testid="sc-alert"]')).toBeNull();
    expect($('[data-testid="storage-card"]')!.dataset.tone).toBe("green");
  });

  it("is amber under 20 %, red under 15 %, and names the hard floor at 10 %", async () => {
    const s = demoSummary("resolved", null)!;
    await mount(<StorageCardView s={{ ...s, freePct: 18, freeGb: 86 }} projectId="p1" {...noop} />);
    expect($('[data-testid="storage-card"]')!.dataset.tone).toBe("amber");
    expect($('[data-testid="sc-alert"]')!.textContent).toContain("under the 20 % notice");
    expect($('[data-testid="sc-alert"]')!.textContent).toContain("nothing is paused");
    unmount();
    await mount(<StorageCardView s={{ ...s, freePct: 12, freeGb: 58 }} projectId="p1" {...noop} />);
    expect($('[data-testid="storage-card"]')!.dataset.tone).toBe("red");
    expect($('[data-testid="sc-alert"]')!.textContent).toContain("under the 15 % critical line");
    unmount();
    await mount(<StorageCardView s={{ ...s, freePct: 15, freeGb: 72 }} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-alert"]')!.textContent).toContain("15 % free, at the 15 % critical line");
    unmount();
    await mount(<StorageCardView s={{ ...s, freePct: 9, freeGb: 43 }} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-alert"]')!.textContent).toContain("10 % hard floor");
    expect($('[data-testid="sc-alert"]')!.textContent).toContain("Builds and upgrades wait");
  });

  it("links the pending approval, switches hosts, and toggles a rule through the caller", async () => {
    const onHost = vi.fn();
    const onRule = vi.fn();
    const onClean = vi.fn();
    const s = demoSummary("asking", null)!;
    await mount(<StorageCardView s={s} projectId="p1" pendingDecisionId="dec_1" onHost={onHost} onRule={onRule} onClean={onClean} cleaning={false} />);
    expect($('[data-testid="sc-pending"]')!.textContent).toContain("1 approval waiting");
    expect($('[data-testid="sc-pending"]')!.getAttribute("href")).toContain("decision=dec_1");
    expect($('[data-testid="sc-docker"]')!.textContent).toContain("Docker VM 66 GB unreclaimed");
    await click($$('[data-testid="sc-host"] .sc-host-name')[1]);
    expect(onHost).toHaveBeenCalledWith("boss-1");
    await click($$('[data-testid="sc-rule-toggle"]')[0]);
    expect(onRule).toHaveBeenCalledWith(expect.objectContaining({ id: "r_backups", enabled: true }));
    await click($('[data-testid="sc-clean"]'));
    expect(onClean).toHaveBeenCalled();
    expect($('[data-testid="sc-thresholds-note"]')).not.toBeNull();
  });

  it("scales segments and lines to the disk", () => {
    const t = (key: string, gbv: number) => ({ key, label: key.toUpperCase(), gb: gbv, pct: 0, rebuildable: false });
    expect(segments({ totalGb: 100, byType: [t("a", 50), t("b", 0), t("free", 50)] })).toEqual([{ key: "a", label: "A", gb: 50, pct: 50 }]);
    expect(segments({ totalGb: 0, byType: [t("a", 50)] })).toEqual([]);
    expect(forecastLines({ trend: [{ at: "", usedGb: 90, freeGb: 10 }], totalGb: 100, freeGb: 10, growthGbPerDay: 5, withRulesDays: 100 }).flat.slice(0, 3)).toEqual([90, 95, 100]);
    expect(forecastLines({ trend: [], totalGb: 100, freeGb: 10, growthGbPerDay: 0, withRulesDays: null }).rules.every((v) => v === 90)).toBe(true);
    expect(linePath([0, 100], 100, 0, 100, 100)).toBe("M0.0,97.0L100.0,3.0");
    expect(linePath([50, 100], 100, 0, 100, 100, 50)).toBe("M0.0,97.0L100.0,3.0");
    expect(linePath([1], 100, 0, 100, 100)).toBe("");
  });
});

describe("where Storage lives", () => {
  it("is a Govern tab under More, with the report at ?incident= and the host drawer linking to it", () => {
    const govern = codeOnly(join(__dirname, "../app/govern/govern-home.tsx"));
    expect(govern).toMatch(/key: "infrastructure", label: "Infrastructure"/);
    expect(govern).toContain("<StorageIncidentReport");
    expect(govern).toContain("<StorageCard");
    const drawer = codeOnly(join(__dirname, "../app/infra/host-drawer.tsx"));
    expect(drawer).toMatch(/tab: "infrastructure", host: h\.id/);
  });
});
