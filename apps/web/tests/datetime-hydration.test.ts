import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import React from "react";

import {
  formatAgeCompact,
  formatDate,
  formatDateTime,
  formatDateTimeShort,
  formatRelativeTime,
  formatTime,
} from "../lib/datetime";
import { MissionTaskBoard } from "../app/mission-operations-ui";
import type { TaskFeedItem } from "../lib/tasks";
import type { ReceiptFeedItem } from "../lib/receipts";
import type { OpsQueueJobItem } from "@boss/api-client";

/**
 * BOSS-346: React #418 (HTML mismatch) was driven by locale/TZ-dependent date
 * strings and Date.now()-based relative labels in SSR'd client islands.
 * These helpers must emit identical text on Node (SSR) regardless of process TZ.
 */

describe("datetime helpers are hydration-safe", () => {
  const iso = "2026-08-12T01:25:15.648620+00:00";

  it("formatDateTime is stable under TZ=UTC and TZ=Asia/Tokyo", () => {
    const previous = process.env.TZ;
    try {
      process.env.TZ = "UTC";
      const utc = formatDateTime(iso);
      process.env.TZ = "Asia/Tokyo";
      const tokyo = formatDateTime(iso);
      process.env.TZ = "America/Los_Angeles";
      const la = formatDateTime(iso);
      expect(utc).toBe(tokyo);
      expect(utc).toBe(la);
      expect(utc).toBe("Aug 12, 2026, 1:25 AM UTC");
    } finally {
      if (previous === undefined) delete process.env.TZ;
      else process.env.TZ = previous;
    }
  });

  it("formatDate / formatTime / formatDateTimeShort are UTC and deterministic", () => {
    expect(formatDate(iso)).toBe("Aug 12, 2026");
    expect(formatTime(iso)).toBe("01:25:15 AM UTC");
    expect(formatDateTimeShort(iso)).toBe("Aug 12, 1:25 AM UTC");
    expect(formatDateTime(undefined)).toBe("Unknown time");
    expect(formatDate(null)).toBe("Never");
  });

  it("formatRelativeTime uses explicit nowMs (no hidden Date.now during pure call)", () => {
    const now = Date.parse("2026-08-12T01:40:15.648Z"); // +15m
    expect(formatRelativeTime(iso, now)).toBe("15m ago");
    expect(formatRelativeTime(iso, now, { style: "short" })).toBe("15m");
    const later = Date.parse("2026-08-12T02:30:15.648Z"); // +65m
    expect(formatRelativeTime(iso, later)).toBe("1h ago");
    expect(formatAgeCompact(iso, later)).toBe("1h 5m");
    expect(formatRelativeTime(undefined, now)).toBe("not yet");
  });

  it("MissionTaskBoard SSR markup embeds the same UTC timestamp the client will paint", () => {
    const tasks: TaskFeedItem[] = [
      {
        id: "tsk_abc123",
        title: "Hydration canary",
        status: "in_progress",
        ownerRoleId: "pm",
        dependencyTaskIds: [],
        priority: "high",
        riskTier: 1,
        updatedAt: iso,
      },
    ];
    const jobs: OpsQueueJobItem[] = [];
    const receipts: ReceiptFeedItem[] = [];
    const html = renderToStaticMarkup(
      React.createElement(MissionTaskBoard, { tasks, jobs, receipts }),
    );
    expect(html).toContain(`dateTime="${iso}"`);
    expect(html).toContain("Aug 12, 2026, 1:25 AM UTC");
    // Must not emit a bare locale-only short form that would diverge by TZ.
    expect(html).not.toMatch(/>\s*Aug 12, 2026, 1:25 AM\s*</);
  });
});
