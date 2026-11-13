// @vitest-environment happy-dom
/** Spend › This month, reconciled (BOSSAPP-864): estimated, reconciled, seat fees and total. */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReconciliationCard, invoiceLines } from "../app/govern/reconciliation-card";
import { $, mount, settle, unmount } from "./_helpers/live-render";

const BODY = { estimated_usd_micros: 55_000_000, reconciled_usd_micros: 67_400_000, fees_usd_micros: 250_000_000,
  total_usd_micros: 317_400_000, by_provider: { anthropic: { invoice_lines: 1 }, openai: { invoice_lines: 0 } } };

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("the reconciliation card", () => {
  it("shows estimated, reconciled, fees and total", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(BODY), { status: 200, headers: { "Content-Type": "application/json" } })));
    await mount(<ReconciliationCard projectId="p1" />);
    await settle();
    expect($('[data-testid="gv-recon-estimated"]')?.textContent).toBe("$55.00");
    expect($('[data-testid="gv-recon-reconciled"]')?.textContent).toBe("$67.40");
    expect($('[data-testid="gv-recon-fees"]')?.textContent).toBe("$250.00");
    expect($('[data-testid="gv-recon-total"]')?.textContent).toBe("$317.40");
    expect(invoiceLines(BODY)).toBe(1);
  });

  it("draws nothing when the read fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 403 })));
    await mount(<ReconciliationCard projectId="p1" />);
    await settle();
    expect($('[data-testid="gv-reconciliation"]')).toBeNull();
  });
});
