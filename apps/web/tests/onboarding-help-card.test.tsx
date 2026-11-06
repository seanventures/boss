import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OnboardingHelpCard, OnboardingHelpRows } from "../app/onboarding-help-card";

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
  // REWRITTEN. These asserted the stale copy: an "Ask" button the composer no longer has,
  // research landing in Discovery and builds in Meetings (both hidden from the rail; Build goes
  // to Workflow), and "merge state is not tracked" beside a PR approval that merges. What they
  // protect is unchanged: how to start, where the work appears, and the honest limits.
  it("tells a first-time operator the ways in, named as the composer names them", () => {
    expect(html).toContain('data-testid="console-capabilities-card"');
    expect(html).toMatch(/<b>Build<\/b>/);
    expect(html).toMatch(/<b>Talk to BOSS<\/b>/);
    // Where the work appears — areas the rail actually shows.
    expect(html).toMatch(/<b>Workflow<\/b>/);
    expect(html).toMatch(/<b>Code Tasks<\/b>/);
    expect(html).toMatch(/<b>Approvals<\/b>/);
  });

  it("names only areas an operator can find in the rail", () => {
    for (const offNav of [/Discovery/, /Meetings/, /CI\/CD/, /Budget area/, /Agents page/]) {
      expect(html).not.toMatch(offNav);
    }
  });

  it("states the limits instead of implying completeness", () => {
    expect(html).toMatch(/host and a target/i);      // deploys need infrastructure first
    expect(html).toContain("<b>not guaranteed</b>"); // previews and sandboxes can fail closed
    // And it does not contradict Approvals, which merges a PR and ships a deploy on approval.
    expect(html).not.toMatch(/not tracked/);
    expect(html).not.toMatch(/paused in Community/);
  });

  it("links to the published source guides and keeps new-tab navigation safe", () => {
    for (const guide of ["GETTING_STARTED.md", "FEEDBACK_LOOP.md"]) {
      expect(html).toContain(`href="https://github.com/seanventures/boss/blob/main/docs/community/${guide}"`);
      expect(() => readFileSync(resolve(__dirname, "../../../docs/community", guide), "utf8")).not.toThrow();
    }
    expect((html.match(/rel="noopener noreferrer"/g) ?? []).length).toBe(2);
    expect(html).not.toContain("<code>docs/community/");
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

describe("the inline form, before the first run (command-center spec §9 row 47)", () => {
  const rows = renderToStaticMarkup(<OnboardingHelpRows onMore={() => {}} />);

  it("is three short rows, each with its old sentence as the tooltip, and the way to the rest", () => {
    expect(rows).toContain('data-testid="console-capabilities-card"');
    for (const name of ["Build", "Talk to BOSS", "Approvals"]) expect(rows).toContain(`<b>${name}</b>`);
    expect((rows.match(/<li title="/g) ?? []).length).toBe(3);
    expect(rows).toContain("Getting started");
    expect(rows).not.toContain("Getting started ↗");
    expect(rows).toContain('aria-haspopup="dialog"');
  });

  it("names only areas an operator can find in the rail", () => {
    for (const offNav of [/Discovery/, /Meetings/, /CI\/CD/, /Budget area/, /Agents page/]) {
      expect(rows).not.toMatch(offNav);
    }
  });
});
