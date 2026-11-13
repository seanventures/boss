/**
 * ROUTINES (BOSSAPP-877): a schedule's Rules — cron said wrong before it is sent, the draft
 * carries what the schedule stores (cron, if-still-running, missed, skip when quiet).
 */
import { describe, expect, it } from "vitest";

import { CATCH_UP_WORDS, CONCURRENCY_WORDS, cronProblem, scheduleDraftFor } from "../app/workflows/library";

describe("a schedule's rules", () => {
  it("says a cron is wrong before it is sent", () => {
    expect(cronProblem("")).toBeNull();
    expect(cronProblem("0 9 * * 1-5")).toBeNull();
    expect(cronProblem("*/15 * * jan,feb mon")).toBeNull();
    expect(cronProblem("0 9 * *")).toContain("five fields");
    expect(cronProblem("0 9 * * $")).toContain("Use numbers");
  });

  it("opens a stored routine with its rules, and a new one with the defaults", () => {
    const fresh = scheduleDraftFor({ workflow_key: "digest", name: "Digest" });
    expect([fresh.cron, fresh.concurrency, fresh.catchUp, fresh.gate]).toEqual(["", "always_enqueue", "one", false]);
    const stored = scheduleDraftFor({ workflow_key: "digest", name: "Digest" }, {
      id: "wsc_1", workflow_key: "digest", name: "Digest", describes: "on cron '0 9 * * 1-5' (UTC)",
      enabled: true, next_run_at: null, last_run_at: null, last_error: null,
      cron_expr: "0 9 * * 1-5", concurrency_policy: "coalesce", catch_up: "all",
      activity_gate: { kinds: ["work_item."], min_events: 1 },
    });
    expect([stored.cron, stored.concurrency, stored.catchUp, stored.gate]).toEqual(["0 9 * * 1-5", "coalesce", "all", true]);
    expect(CONCURRENCY_WORDS.coalesce).toBe("Run once after");
    expect(Object.keys(CATCH_UP_WORDS)).toEqual(["none", "one", "all"]);
  });
});
