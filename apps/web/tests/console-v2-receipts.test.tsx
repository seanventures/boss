import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ReceiptsScreen } from "../components/v2/ReceiptsScreen";
import type { ReceiptFeed, V2Receipt } from "../lib/v2/receipts";

/**
 * The Receipts screen.
 *
 * "Verified today: 100" was the old console's page size wearing a total's name. And a receipt
 * left at in_progress by an API restart is neither a success nor a failure — rolling it into
 * "done" reports a clean day for a day that lost work. Both are tested here.
 */

const rec = (over: Partial<V2Receipt> = {}): V2Receipt => ({
  id: "rcp_1",
  effect: "deployment.apply",
  scope: "Shorty",
  status: "succeeded",
  unfinished: false,
  provider_ref: "dep_88",
  error: null,
  data_boundary: "internal",
  created_at: "2026-08-26T11:30:00Z",
  ...over,
});

const feed = (over: Partial<ReceiptFeed> = {}): ReceiptFeed => ({
  project_id: "boss-dogfood",
  window_hours: 24,
  items: [rec()],
  window_total: 1,
  window_unfinished: 0,
  window_failed: 0,
  shown: 1,
  apiReachable: true,
  ...over,
});

const html = (f: ReceiptFeed) => renderToStaticMarkup(<ReceiptsScreen feed={f} />);

describe("every number says what it counts", () => {
  it("the window count is labelled with its window", () => {
    // Unlabelled, "12,402" could be all time, today, or the page. Only one is true.
    const out = html(feed({ window_total: 12402, shown: 60 }));
    expect(out).toContain('data-testid="v2-receipts-window-total"');
    expect(out).toMatch(/12,402/);
    expect(out).toMatch(/in 24h/);
  });

  it("says when the list is smaller than the window", () => {
    // This is the "Verified today: 100" defect: a page read as a total.
    const out = html(feed({ window_total: 12402, shown: 60 }));
    expect(out).toContain('data-testid="v2-receipts-shown"');
    expect(out).toMatch(/showing the 60 most recent/);
  });

  it("does not nag when the list IS the window", () => {
    expect(html(feed({ window_total: 3, shown: 3 })))
      .not.toContain('data-testid="v2-receipts-shown"');
  });
});

describe("unfinished is its own state", () => {
  it("a stalled receipt is shown as never finished, not as in progress", () => {
    // "in_progress" from three days ago is not in progress. It is lost work.
    const out = html(feed({
      items: [rec({ id: "rcp_x", status: "in_progress", unfinished: true })],
      window_unfinished: 1,
    }));
    expect(out).toContain('data-unfinished="true"');
    // Scoped to the ROW. An unscoped match also hits the header's "1 never finished", so it
    // passed even when the row rendered the raw status — the assertion was not testing the row.
    const row = out.slice(out.indexOf('data-testid="v2-receipt-rcp_x"'));
    expect(row).toMatch(/never finished/);
    expect(row.slice(0, row.indexOf("</span>") + 7)).not.toMatch(/in_progress/);
  });

  it("unfinished and failed are counted separately", () => {
    // One lost the work; the other refused it. Different problems, different responses.
    const out = html(feed({ window_total: 100, window_unfinished: 4, window_failed: 7, shown: 60 }));
    expect(out).toContain('data-testid="v2-receipts-unfinished"');
    expect(out).toContain('data-testid="v2-receipts-failed"');
  });

  it("a clean window shows neither alarm", () => {
    const out = html(feed({ window_unfinished: 0, window_failed: 0 }));
    expect(out).not.toContain('data-testid="v2-receipts-unfinished"');
    expect(out).not.toContain('data-testid="v2-receipts-failed"');
  });
});

describe("evidence, not claims", () => {
  it("shows the provider reference", () => {
    expect(html(feed())).toContain('data-testid="v2-receipt-ref-rcp_1"');
  });

  it("says so when a receipt cannot be independently checked", () => {
    // A receipt with no provider ref is an assertion about work, not evidence of it. Rendering
    // it identically to a verifiable one overstates what is known.
    const out = html(feed({ items: [rec({ id: "rcp_n", provider_ref: null })] }));
    expect(out).toContain('data-testid="v2-receipt-noref-rcp_n"');
    expect(out).toMatch(/cannot be independently checked/);
  });

  it("a failure carries its message", () => {
    const out = html(feed({ items: [rec({ id: "rcp_f", status: "failed", error: "host unreachable" })] }));
    expect(out).toContain("host unreachable");
  });
});

describe("empty states", () => {
  it("an unreachable API shows no counts at all", () => {
    // A zero here is indistinguishable from a quiet day, and this page exists to show what
    // happened — so it must not show a confident nothing.
    const out = html(feed({ apiReachable: false, blockedReason: "API returned 503", items: [], window_total: 0, shown: 0 }));
    expect(out).toContain('data-testid="v2-receipts-unreachable"');
    expect(out).not.toContain('data-testid="v2-receipts-window-total"');
  });

  it("a genuinely quiet window says so with its window", () => {
    const out = html(feed({ items: [], window_total: 0, shown: 0, window_hours: 6 }));
    expect(out).toContain('data-testid="v2-receipts-none"');
    expect(out).toMatch(/last 6 hours/);
  });
});
