/**
 * AN AREA OPENS BY ITS RAIL LABEL, AND THE SHELL SAYS "ONLINE" WHEN THE API ANSWERS (round 3, W3).
 *
 * The build walk opened `?area=factory` — the rail says "Factory" — and landed on the Command
 * Center, which has no PR, so "the Factory shows the build's PR" failed. Its sidebar said "Control
 * plane offline" on every page while every other read worked: the status read was REFUSED (a
 * server-side POST twin without the console's Origin, fixed in 19192886), and a refusal was
 * reported as an outage.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { consoleParams, RAIL_LABEL_AREAS, railLabelKey } from "../app/legacy-areas";
import { AREA_IDS } from "../lib/area-ids";
import { communityNavCopy } from "../lib/edition";

const SHELL = readFileSync(join(process.cwd(), "app/ConsoleShell.tsx"), "utf8");

/** [id, label] for every rail entry (ConsoleShell AREAS), plus the Community label overrides. */
function railLabels(): [string, string][] {
  const block = SHELL.slice(SHELL.indexOf("const AREAS: AreaDef[] = ["), SHELL.indexOf("];", SHELL.indexOf("const AREAS: AreaDef[] = [")));
  const out: [string, string][] = [];
  for (const m of block.matchAll(/id:\s*"([^"]+)",[\s\S]*?label:\s*"([^"]+)"/g)) out.push([m[1], m[2]]);
  for (const [id] of [...out]) {
    const label = communityNavCopy(id).label;
    if (label) out.push([id, label]);
  }
  return out;
}

const landsOn = (area: string) => consoleParams(new URLSearchParams({ area })).params.get("area");

describe("every rail label opens its area", () => {
  const labels = railLabels();
  it("reads the rail", () => {
    expect(labels.length).toBeGreaterThan(20);
    expect(labels).toContainEqual(["code_tasks", "Code Tasks"]);
    // The old name still lands (BOSSAPP-914: Factory → Code Tasks).
    expect(landsOn("Factory")).toBe("code_tasks");
  });
  for (const [id, label] of labels) {
    it(`“${label}” → ${id}`, () => {
      const want = landsOn(id);           // an id that is itself an alias lands where it does
      for (const typed of [label, label.toLowerCase(), railLabelKey(label), railLabelKey(label).replace(/_/g, "-")]) {
        expect(landsOn(typed), typed).toBe(want);
      }
    });
  }
  it("the table names only real areas, and never re-reads an id", () => {
    for (const id of Object.values(RAIL_LABEL_AREAS)) expect(AREA_IDS).toContain(id);
    for (const id of AREA_IDS) expect(RAIL_LABEL_AREAS[id], id).toBeUndefined();
  });
  it("keeps everything else the link carried", () => {
    const p = consoleParams(new URLSearchParams({ area: "factory", org: "org_1", run: "crun_1" })).params;
    expect(Object.fromEntries(p)).toEqual({ area: "code_tasks", org: "org_1", run: "crun_1" });
  });
  it("an unknown name still falls back to the Command Center", () => {
    expect(landsOn("nowhere")).toBe("overview");
  });
});

describe("the shell's control-plane line", () => {
  it("a refused status read is not an outage; an unreachable API is", async () => {
    vi.resetModules();
    const { BossApiError } = await import("@boss/api-client");
    const mod = await import("../lib/client");
    const spy = vi.spyOn(mod.client, "getStatus");
    const { fetchStatus, controlPlaneOnline } = await import("../lib/status");

    spy.mockRejectedValueOnce(new BossApiError("refused", 403, { error: "csrf_failed" }));
    const refused = await fetchStatus("p1");
    expect(refused.apiReachable).toBe(false);
    expect(controlPlaneOnline(refused)).toBe(true);

    spy.mockRejectedValueOnce(new BossApiError("broken", 502));
    expect(controlPlaneOnline(await fetchStatus("p1"))).toBe(false);

    spy.mockRejectedValueOnce(new TypeError("fetch failed"));
    expect(controlPlaneOnline(await fetchStatus("p1"))).toBe(false);
    spy.mockRestore();
  });
  it("the page hands the shell controlPlaneOnline(status), not apiReachable alone", () => {
    const page = readFileSync(join(process.cwd(), "app/page.tsx"), "utf8");
    expect(page).toMatch(/apiReachable: controlPlaneOnline\(status\)/);
  });
});
