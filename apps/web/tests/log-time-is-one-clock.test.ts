/**
 * One clock for every log (spec §4.3): the step panel said `18:02` in UTC, the Log `Oct 4,
 * 2026, 6:01 PM UTC`, the river `09:42` local. They now all read the reader's local time — the
 * Log to the second — with the same UTC instant in the tooltip and the same day words.
 */
import { describe, expect, it } from "vitest";

import { clock, dayLabel, utcTitle } from "../lib/log-time";
import { clock as riverClock, dayLabel as riverDay } from "../app/agents/activity-river";
import { clock as stepClock, stepFacts } from "../app/workflows/step-run";

const AT = "2026-10-04T18:02:41.512Z";

describe("one instant reads the same everywhere", () => {
  it("the Log, the river and the step panel agree (seconds aside)", () => {
    const local = new Date(AT);
    const hm = `${String(local.getHours()).padStart(2, "0")}:${String(local.getMinutes()).padStart(2, "0")}`;
    expect(clock(AT)).toBe(`${hm}:${String(local.getSeconds()).padStart(2, "0")}`);
    expect(riverClock(AT)).toBe(hm);
    expect(stepClock(AT)).toBe(hm);
    expect(clock(AT, { seconds: false })).toBe(hm);
  });

  it("a step card's 'Started' time is local, and never claims to be UTC (BOSSAPP-615)", () => {
    const local = new Date(AT);
    const hm = `${String(local.getHours()).padStart(2, "0")}:${String(local.getMinutes()).padStart(2, "0")}`;
    const [facts] = [stepFacts({ started_at: AT })];
    expect(facts).toEqual([`Started ${hm}`]);
    expect(facts[0]).not.toMatch(/Z$/);
  });

  it("the tooltip is the UTC instant", () => {
    expect(utcTitle(AT)).toBe("2026-10-04T18:02:41.512Z");
    expect(utcTitle("2026-10-04T18:02:41Z")).toBe("2026-10-04T18:02:41Z");
    expect(utcTitle("not a time")).toBe("");
  });

  it("days have one set of words", () => {
    const now = new Date("2026-10-04T12:00:00");
    expect(dayLabel("2026-10-04T08:00:00", now)).toBe("Today");
    expect(dayLabel("2026-10-03T08:00:00", now)).toBe("Yesterday");
    expect(dayLabel("2026-09-29T10:00:00", now)).toBe("Tue 29 Sep");
    expect(riverDay("2026-09-29T10:00:00", now)).toBe(dayLabel("2026-09-29T10:00:00", now));
  });
});
