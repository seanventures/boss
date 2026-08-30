import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OnboardingHelpCard } from "../app/onboarding-help-card";

describe("console capabilities help card", () => {
  const html = renderToStaticMarkup(<OnboardingHelpCard />);

  /**
   * This card used to be design-partner SETUP — `make onboard`, `make seed`, Plane keys — and
   * these tests asserted that copy. It was setup instructions on a page you only reach after
   * setup has already succeeded, so it was replaced with the question a first-time operator
   * actually has: what can I do here, and what is not here yet.
   *
   * The tests moved with it. What they protect is unchanged: the card must tell a newcomer how
   * to start, and it must not promise anything BOSS cannot do.
   */
  it("tells a first-time operator the three ways in", () => {
    expect(html).toContain('data-testid="console-capabilities-card"');
    // Every entry point, named, with where its output lands — a way in that does not say where
    // the work appears leaves the operator watching the wrong area.
    expect(html).toMatch(/<b>Ask<\/b>/);
    expect(html).toMatch(/<b>Research<\/b>/);
    expect(html).toMatch(/<b>Build<\/b>/);
    expect(html).toMatch(/Discovery/);
    expect(html).toMatch(/Meetings/);
    expect(html).toMatch(/Factory/);
  });

  it("states what is missing instead of implying completeness", () => {
    expect(html).toMatch(/not here yet/i);
    // The three real gaps. Each was a live surprise for an operator before it was written down.
    expect(html).toMatch(/host and a target/i);      // deploys need infrastructure first
    expect(html).toMatch(/<b>not tracked<\/b>/);      // merge state is not known
    expect(html).toContain("<b>not guaranteed</b>"); // previews and sandboxes can fail closed
  });

  it("does not overclaim", () => {
    expect(html).not.toMatch(/sandboxes? (and previews )?always work/i);
    expect(html).not.toMatch(/public open-source (release|flip) is (complete|done)/i);
    // Naming a paid-only surface here would sell a Community reader something they do not have.
    // The card avoids the problem by describing capability, not edition.
    for (const paid of [/Account Connect/i, /hosted marketplace/i, /enterprise clusters/i]) {
      expect(html).not.toMatch(paid);
    }
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
