import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

import ConsoleShell from "../app/ConsoleShell";
import { isCommunityPrimaryNavVisible } from "../lib/edition";

const HIDDEN_FROM_COMMUNITY_NAV = [
  "infra",
  "metatron",
  "studio",
  "sandboxes",
  "github",
  "ops",
  "org",
  "foundations",
  "components",
  "pipeline",
  "comms",
  "assurance",
  "capabilities",
  "agent",
  "dashboard",
  "portfolio",
  "discovery",
  "meetings",
  "processes",
  "shepherd",
  "delivery",
  "monitoring",
] as const;

function read(rel: string): string {
  return readFileSync(resolve(__dirname, rel), "utf8");
}

function areaIdUnion(source: string): string[] {
  const match = source.match(/type AreaId =([\s\S]*?);/);
  expect(match, "AreaId union").toBeTruthy();
  return [...(match?.[1].matchAll(/"([a-z_]+)"/g) ?? [])].map((m) => m[1]);
}

function areasListIds(source: string): string[] {
  return [...source.matchAll(/\{\s*id:\s*"([a-z_]+)"/g)].map((m) => m[1]);
}

describe("Community UI v1 cut", () => {
  it("keeps UI_V1_CUT.md as the nav contract", () => {
    const cut = readFileSync(
      resolve(__dirname, "../../../docs/product/UI_V1_CUT.md"),
      "utf8",
    );
    expect(cut).toMatch(/Not public GA/);
    expect(cut).toMatch(/Cloud Easy Connect/);
    expect(cut).toMatch(/Areas are not deleted/);
  });

  it("does not show Cloud Easy Connect un-gated on Community", () => {
    const page = read("../app/page.tsx");
    const gateOpen = page.indexOf(
      'title="BOSS Cloud Easy Connect"',
    );
    const gateClose = page.indexOf("</PaidSurfaceGate>", gateOpen);
    const easyConnect = page.indexOf('data-testid="cloud-easy-connect"');
    const device = page.indexOf("Connect with device code");
    const browser = page.indexOf("Connect in browser");

    expect(gateOpen).toBeGreaterThan(-1);
    expect(gateClose).toBeGreaterThan(gateOpen);
    expect(easyConnect).toBeGreaterThan(gateOpen);
    expect(easyConnect).toBeLessThan(gateClose);
    expect(device).toBeGreaterThan(gateOpen);
    expect(device).toBeLessThan(gateClose);
    expect(browser).toBeGreaterThan(gateOpen);
    expect(browser).toBeLessThan(gateClose);
    expect(page).toContain('surface="account_connect"');

    // Community nav itself never surfaces the Cloud CTA.
    const html = renderToStaticMarkup(
      React.createElement(ConsoleShell, {
        status: {
          apiReachable: true,
          budgetState: "green",
          deadletters: 0,
          pendingDecisions: 0,
        },
        scope: { project: "boss-dogfood", actor: "ceo" },
        editionId: "community",
        children: React.createElement("div", null, "body"),
      }),
    );
    expect(html).not.toMatch(/Cloud Easy Connect/i);
    expect(html).not.toContain("Connect with device code");
    expect(html).not.toContain("Connect in browser");
    expect(html).not.toContain('data-testid="cloud-easy-connect"');
  });

  it("AreaId still includes hidden Community areas", () => {
    const shell = read("../app/ConsoleShell.tsx");
    const union = areaIdUnion(shell);
    const listed = areasListIds(shell);

    for (const area of HIDDEN_FROM_COMMUNITY_NAV) {
      expect(union, `AreaId missing ${area}`).toContain(area);
      expect(listed, `AREAS missing ${area}`).toContain(area);
      expect(isCommunityPrimaryNavVisible(area, "community")).toBe(false);
      expect(isCommunityPrimaryNavVisible(area, "enterprise")).toBe(true);
    }
  });
});
