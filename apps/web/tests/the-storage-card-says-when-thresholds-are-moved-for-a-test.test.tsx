// @vitest-environment happy-dom
/**
 * `boss qa disk-pressure` moves the storage thresholds for a test (ENT-REC-29..31). The card says
 * so, and until when, so nobody reads a test's warning as the disk's.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { StorageCardView } from "../app/infra/storage-card";
import { parseSummary } from "../lib/infra-storage";
import { demoSummary } from "../lib/infra-storage-demo";
import { $, mount, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

const noop = { onHost: () => {}, onClean: () => {}, onRule: () => {}, cleaning: false };

describe("thresholds moved for a test", () => {
  it("parses the mark from the summary, and none when there is none", () => {
    expect(parseSummary({ qa_pressure: { level: "critical", until: "2026-11-28T10:00:00+00:00", by: "usr_t" } })!.qaPressure)
      .toEqual({ level: "critical", until: "2026-11-28T10:00:00+00:00", by: "usr_t" });
    expect(parseSummary({ qa_pressure: null })!.qaPressure).toBeNull();
  });

  it("shows the line with its level, its end and how to end it now", async () => {
    const s = { ...demoSummary("resolved", null)!, qaPressure: { level: "warn", until: "2026-11-28T10:00:00Z", by: "usr_t" } };
    await mount(<StorageCardView s={s} projectId="p1" {...noop} />);
    const line = $('[data-testid="sc-qa-pressure"]')!;
    expect(line.textContent).toContain("Thresholds moved for a test");
    expect(line.textContent).toContain("(warn) until");
    expect(line.textContent).toContain("boss qa disk-pressure off");
  });

  it("is absent on an ordinary day", async () => {
    await mount(<StorageCardView s={demoSummary("resolved", null)!} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-qa-pressure"]')).toBeNull();
  });
});
