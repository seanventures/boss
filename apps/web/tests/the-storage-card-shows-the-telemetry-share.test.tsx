// @vitest-environment happy-dom
/**
 * ST-8 (BOSSAPP-1153): the Storage card says how much of the database is telemetry and what the
 * last retention sweep removed. Read from the same summary (`telemetry`); a summary without it
 * (an older API) draws no row and never crashes.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { StorageCardView, telemetryWords } from "../app/infra/storage-card";
import { parseSummary, parseTelemetry } from "../lib/infra-storage";
import { demoSummary } from "../lib/infra-storage-demo";
import { $, mount, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

const noop = { onHost: () => {}, onClean: () => {}, onRule: () => {}, cleaning: false };

describe("the telemetry share on the Storage card", () => {
  it("draws the share and the last sweep from the summary", async () => {
    const s = demoSummary("resolved", null)!;
    expect(s.telemetry).not.toBeNull();
    await mount(<StorageCardView s={s} projectId="p1" {...noop} />);
    const row = $('[data-testid="sc-telemetry"]')!;
    expect(row.textContent).toContain("1.6 GB");
    expect(row.textContent).toContain("21 % of the database");
    expect($('[data-testid="sc-telemetry-sweep"]')!.textContent).toContain("182,340 rows");
    expect(row.getAttribute("title")).toContain("events");
  });

  it("is absent when the API does not send it, and odd shapes never crash", async () => {
    const wire = { host_id: "h", disk: { total_gb: 100, free_gb: 50 } };
    const s = parseSummary(wire)!;
    expect(s.telemetry).toBeNull();
    await mount(<StorageCardView s={s} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-telemetry"]')).toBeNull();
    expect(parseTelemetry(null)).toBeNull();
    expect(parseTelemetry([])).toBeNull();
    expect(parseTelemetry({ tables: [{}], last_sweep: {} })).toMatchObject({ gb: 0, sharePct: null, tables: [], lastSweep: null });
  });

  it("says a sweep that found nothing, and one that did not finish", () => {
    const base = { gb: 0.5, sharePct: 10, tables: [] };
    expect(telemetryWords({ ...base, lastSweep: null }).sweep).toBe("no sweep yet");
    const at = new Date().toISOString();
    expect(telemetryWords({ ...base, lastSweep: { at, rows: 0, bytes: 0, partitions: [], archived: 0, errors: [], receiptId: null } }).sweep).toContain("nothing past retention");
    expect(telemetryWords({ ...base, lastSweep: { at, rows: 5, bytes: 1, partitions: [], archived: 0, errors: ["receipts: timeout"], receiptId: null } }).sweep).toContain("1 table not finished");
  });
});
