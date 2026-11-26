// @vitest-environment happy-dom
/**
 * BOSSAPP-1459: the Storage card shows where the space goes by category, against the one budget
 * (engine/boss_engine/services/disk_budget.py), and warns BEFORE a budget is reached: a category
 * near its line or past it comes first, with what to do. Unmeasured categories are left out; a
 * summary without the block (an older API) draws nothing and never crashes.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { StorageCardView, budgetRows } from "../app/infra/storage-card";
import { parseBudget, parseSummary } from "../lib/infra-storage";
import { $, $$, mount, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

const noop = { onHost: () => {}, onClean: () => {}, onRule: () => {}, cleaning: false };

const wire = {
  host_id: "h", disk: { total_gb: 100, free_gb: 50 },
  budget: {
    warn_at_pct: 80, total: { gb: 21.5, budget_gb: 52 },
    rows: [
      { key: "images", label: "BOSS images", gb: 7.5, budget_gb: 10, level: "ok", bounded_by: "one set", what: "", action: "" },
      { key: "backups", label: "Backups", gb: 6.4, budget_gb: 5, level: "over", bounded_by: "the cap",
        what: "Backups: 6.4 GB, over the 5 GB budget.", action: "Approve the backup decision in Decisions." },
      { key: "sandboxes", label: "Sandboxes", gb: 2.6, budget_gb: 3, level: "near", bounded_by: "the reaper",
        what: "Sandboxes: 2.6 GB, near the 3 GB budget.", action: "Nothing to do: BOSS removes them by itself." },
      { key: "logs", label: "Containers and logs", gb: null, budget_gb: 2, level: "unknown", bounded_by: "compose", what: "", action: "" },
    ],
  },
};

describe("the disk budget on the Storage card", () => {
  it("lists the categories near or over first, each with what to do", async () => {
    const s = parseSummary(wire)!;
    expect(s.budget).not.toBeNull();
    await mount(<StorageCardView s={s} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-budget"]')!.textContent).toContain("22 GB of 52 GB");
    expect($('[data-testid="sc-budget-chip"]')!.textContent).toBe("2 near or over");
    const rows = $$('[data-testid="sc-budget-row"]');
    expect(rows.map((r) => r.getAttribute("data-level"))).toEqual(["over", "near", "ok"]);
    expect(rows[0].textContent).toContain("Backups");
    expect(rows[0].textContent).toContain("Approve the backup decision in Decisions.");
    expect(rows[2].querySelector('[data-testid="sc-budget-action"]')).toBeNull();
  });

  it("marks an overdue host sample and explains how to recover the helper", async () => {
    const s = parseSummary({ ...wire, stale: true, helper_overdue: true, sampled_at: "2026-11-25T18:00:00Z" })!;
    await mount(<StorageCardView s={s} projectId="p1" {...noop} />);
    const alert = $('[data-testid="sc-helper-offline"]')!.textContent;
    expect(alert).toContain("boss steward status");
    expect(alert).toContain("boss steward install");
    expect($('[data-testid="sc-tile-free"]')!.textContent).toContain("Last reported free space");
  });

  it("leaves out what nobody measured, and says all within when nothing is near", () => {
    const b = parseBudget({ ...wire.budget, rows: [wire.budget.rows[0], wire.budget.rows[3]] })!;
    expect(budgetRows(b).map((r) => r.key)).toEqual(["images"]);
  });

  it("is absent when the API does not send it, and odd shapes never crash", async () => {
    const s = parseSummary({ host_id: "h", disk: { total_gb: 100, free_gb: 50 } })!;
    expect(s.budget).toBeNull();
    await mount(<StorageCardView s={s} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-budget"]')).toBeNull();
    expect(parseBudget(null)).toBeNull();
    expect(parseBudget([])).toBeNull();
    expect(parseBudget({ rows: [{}] })).toBeNull();
    expect(parseBudget({ rows: [{ key: "x", level: "weird" }] })!.rows[0].level).toBe("unknown");
  });
});
