import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OnboardingHelpCard } from "../app/onboarding-help-card";

describe("design-partner onboarding help card", () => {
  const html = renderToStaticMarkup(<OnboardingHelpCard />);

  it("renders an honest Command Center help card", () => {
    expect(html).toContain('data-testid="design-partner-onboarding-card"');
    expect(html).toContain("Design-partner setup");
    expect(html).toContain("docs/operations/DESIGN_PARTNER_ONBOARDING.md");
    expect(html).toContain("make onboard");
    expect(html).toContain("make smoke");
    expect(html).toContain("make qa-live");
  });

  it("distinguishes Community from Cloud and does not overclaim", () => {
    expect(html).toMatch(/Community/i);
    expect(html).toMatch(/Cloud/i);
    expect(html).toMatch(/private/i);
    expect(html).not.toMatch(/sandboxes? (and previews )?always work/i);
    expect(html).toContain("not</strong> guaranteed");
    expect(html).not.toMatch(/public open-source (release|flip) is (complete|done)/i);
    expect(html).toContain("Do not invent");
    expect(html).toContain("Plane");
  });

  it("is mounted on overview only — not sold as Cloud or Account Connect", () => {
    const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    expect(page).toContain("<OnboardingHelpCard");
    const mount = page.slice(
      page.lastIndexOf("<section", page.indexOf("<OnboardingHelpCard")),
      page.indexOf("<OnboardingHelpCard") + 80,
    );
    expect(mount).toContain('data-area="overview"');
    expect(mount).not.toMatch(/Account Connect/i);
    expect(page.split("<OnboardingHelpCard").length - 1).toBe(1);
    const otherAreas = ["code_tasks", "decisions", "org", "ops", "enterprise"];
    for (const area of otherAreas) {
      const areaIdx = page.indexOf(`data-area="${area}"`);
      expect(areaIdx).toBeGreaterThan(-1);
    }
    expect(page).not.toMatch(
      /data-area="(?!overview")[^"]*"[\s\S]{0,240}<OnboardingHelpCard/,
    );
  });
});
