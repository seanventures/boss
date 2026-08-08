import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("BOSS mission-control design system", () => {
  const css = readFileSync(resolve(__dirname, "../app/design-system.css"), "utf8");
  const shell = readFileSync(resolve(__dirname, "../app/ConsoleShell.tsx"), "utf8");
  const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
  const operations = readFileSync(
    resolve(__dirname, "../app/mission-operations-ui.tsx"),
    "utf8",
  );
  const providerConfiguration = readFileSync(
    resolve(__dirname, "../app/provider-configuration-ui.tsx"),
    "utf8",
  );

  it("defines the approved foundations", () => {
    expect(css).toContain("--void: #090d17");
    expect(css).toContain("--teal: #2ec5c5");
    expect(css).toContain("--green: #34d399");
    expect(css).toContain("--amber: #f5b544");
    expect(css).toContain("--red: #f2647a");
    expect(css).toContain('"Sora"');
    expect(css).toContain('"Manrope"');
    expect(css).toContain('"JetBrains Mono"');
  });

  it("uses the supplied BOSS mark and an icon library", () => {
    expect(shell).toContain('src="/boss-mark.png"');
    expect(shell).not.toContain('src="/BOSS-logo.png"');
    expect(shell).toContain('from "@phosphor-icons/react"');
    expect(shell).not.toContain("<svg");
  });

  it("implements the operational design-system views", () => {
    expect(page).toContain('className="cc-composer"');
    expect(operations).toContain('className="mission-task-board"');
    expect(operations).toContain('className="ar-table"');
    expect(page).toContain('className="dq"');
    expect(page).toContain('className="mission-evidence-receipt"');
    expect(page).toContain("<MissionAgentRoster");
    expect(page).toContain("<MissionTaskBoard");
    expect(css).toContain(".cc-composer");
    expect(css).toContain(".ar-head");
    expect(css).toContain(".dq-row");
    expect(css).toContain(".mission-task-footer");
    expect(providerConfiguration).toContain('className="provider-subnav"');
    expect(providerConfiguration).toContain('className="provider-table"');
    expect(providerConfiguration).toContain('className="provider-drawer"');
    expect(providerConfiguration).toContain('className="provider-cost-chart"');
    expect(css).toContain(".provider-suite-summary");
    expect(css).toContain(".provider-drawer");
  });

  it("honors reduced-motion preferences", () => {
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  });

  it("keeps provider tables and fixed drawers inside the mobile viewport", () => {
    expect(css).toMatch(
      /\.provider-table-wrap\s*\{[\s\S]*position:\s*relative;[\s\S]*max-width:\s*100%;[\s\S]*overflow-x:\s*auto;/,
    );
    expect(css).toMatch(
      /\.bx-content\[data-active="infra"\][\s\S]*animation:\s*none\s*!important;/,
    );
    expect(css).toContain("width: 100vw;");
  });
});
