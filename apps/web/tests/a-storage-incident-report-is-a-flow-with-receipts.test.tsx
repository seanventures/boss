// @vitest-environment happy-dom
/**
 * S5 · The incident report (BOSSAPP-1156): the flow with times, Freed · Work paused · Lost or
 * repeated = 0 · Cost, the cause, what was learned, what was removed and by whose say-so with
 * the receipts link, the paused work; exported as Markdown or JSON.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { StorageIncidentReportView, minutesWord, reportMarkdown } from "../app/infra/storage-incident-report";
import { demoIncidentDetail } from "../lib/infra-storage-demo";
import { $, $$, mount, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

const d = demoIncidentDetail("si_20261114_laptop")!;

describe("the incident report", () => {
  it("is the flow with times, then four numbers", async () => {
    await mount(<StorageIncidentReportView d={d} />);
    const nodes = $$('[data-testid="sir-flow-node"]');
    expect(nodes.map((n) => n.querySelector(".ifl-cap")!.textContent)).toEqual(["Detected", "Paused", "Cleaned", "Asked", "Removed", "Resumed", "Learned"]);
    expect(nodes.map((n) => n.dataset.key)).toEqual(["detected", "paused", "cleaning", "asking", "freed", "resumed", "reported"]);
    expect(nodes.every((n) => n.querySelector(".ifl-at")?.textContent?.match(/^\d{2}:\d{2}$/))).toBe(true);
    expect(nodes.every((n) => n.dataset.state === "done")).toBe(true);
    expect($('[data-testid="sir-status"]')!.textContent).toContain("Resolved");
    const tiles = $('[data-testid="sir-tiles"]')!.textContent!;
    expect(tiles).toContain("203 GB");
    expect(tiles).toContain("9 % → 61 % free");
    expect(tiles).toContain("19 min");
    expect(tiles).toContain("longest wait 11 min");
    expect($('[data-testid="sir-lost"]')!.textContent).toContain("0");
    expect($('[data-testid="sir-lost"]')!.dataset.tone).toBe("green");
    expect(tiles).toContain("$0.11");
    expect(tiles).toContain("9 tool calls");
  });

  it("says the cause in plain words, what was learned, what was removed by whose say-so, and the paused work", async () => {
    await mount(<StorageIncidentReportView d={d} />);
    expect($('[data-testid="sir-cause"]')!.textContent).toContain("Every upgrade wrote a 1 GB backup");
    expect(document.body.textContent).toContain("no temp-space limit");
    const learned = $$('[data-testid="sir-learned"] .sir-row');
    expect(learned).toHaveLength(2);
    expect(learned[0].textContent).toContain("Rule added");
    expect(learned[0].textContent).toContain("keep 10 · 7 days · 20 GB cap");
    expect(learned[1].textContent).toContain("Proposed");
    expect($('[data-testid="sir-review-rule"]')!.getAttribute("href")).toContain("decision=dec_rule_pgtemp");
    const removed = $$('[data-testid="sir-removed"] .sir-row');
    expect(removed).toHaveLength(8);
    expect(removed[0].textContent).toContain("approved by a person");
    expect(removed[0].textContent).toContain("161 GB");
    expect(removed[1].textContent).toContain("automatic");
    expect(removed.at(-1)!.textContent).toContain("66 GB");
    expect($('[data-testid="sir-receipts"]')!.textContent).toContain("9 receipts");
    expect($$('[data-testid="sir-paused"] .sir-row')).toHaveLength(3);
    expect($('[data-testid="sir-paused"]')!.textContent).toContain("waited 11 min");
    expect($('[data-testid="sir-paused"]')!.textContent).toContain("resumed");
    expect($('[data-testid="sir-all-runs"]')!.textContent).toContain("All 14 runs");
    expect($('[data-testid="sir-back"]')!.getAttribute("href")).toContain("tab=infrastructure");
    expect($('[data-testid="sir-export-md"]')).not.toBeNull();
    expect($('[data-testid="sir-export-json"]')).not.toBeNull();
  });

  it("exports the same report as Markdown", () => {
    const md = reportMarkdown(d);
    expect(md).toMatch(/^# Storage incident · laptop-boss/);
    for (const h of ["## What happened", "## Cause", "## Learned", "## What was removed", "## Paused work"]) expect(md).toContain(h);
    expect(md).toContain("- Freed: 203 GB");
    expect(md).toContain("- Lost or repeated: 0");
    expect(md).toContain("approved by a person");
    expect(md).toContain("Rule added: Pre-upgrade backups");
    expect(minutesWord(19 * 60)).toBe("19 min");
    expect(minutesWord(40)).toBe("40 s");
    expect(minutesWord(65 * 60)).toBe("1 h 05 min");
  });
});
