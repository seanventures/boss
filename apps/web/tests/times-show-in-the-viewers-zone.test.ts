// @vitest-environment happy-dom
/**
 * EVERY TIME IN THE VIEWER'S OWN ZONE (operator, 2026-11-06): "since 3:23 PM" for a wait that began
 * at 7:23 AM in Los Angeles was the console printing UTC. lib/local-time.ts formats in the browser's
 * zone once hydrated (UTC before, so the server's paint matches), and the step clock uses it.
 */
import { describe, expect, it } from "vitest";

import { markHydrated, sinceLocal, stampLocal, viewerZone, whenLocal } from "../lib/local-time";
import { sinceWords, stepClock } from "../lib/step-timing";

const T = Date.parse("2026-11-06T15:23:00Z"); // 7:23 AM in Los Angeles (PST), 3:23 PM UTC
const NOW = Date.parse("2026-11-06T18:00:00Z");

describe("times in the viewer's zone", () => {
  it("a same-day time is the zone's clock time, not UTC's", () => {
    expect(sinceLocal(T, NOW, "America/Los_Angeles")).toBe("7:23 AM");
    expect(sinceLocal(T, NOW, "UTC")).toBe("3:23 PM");
    expect(sinceLocal(T, NOW, "Asia/Kolkata")).toBe("8:53 PM");
  });

  it("an earlier day is the zone's calendar day", () => {
    const late = Date.parse("2026-11-06T02:00:00Z"); // still Nov 5 in Los Angeles
    expect(sinceLocal(late, NOW, "America/Los_Angeles")).toBe("Nov 5");
    expect(sinceLocal(late, NOW, "UTC")).toBe("2:00 AM");
  });

  it("a tooltip stamp names the zone", () => {
    expect(stampLocal(T, "America/Los_Angeles")).toMatch(/Nov 6, 7:23\sAM PST/);
  });

  it("an API time in words stays as it came; an ISO stamp is formatted", () => {
    expect(whenLocal("2h ago", NOW, "America/Los_Angeles")).toBe("2h ago");
    expect(whenLocal("2026-11-06T15:23:00Z", NOW, "America/Los_Angeles")).toBe("7:23 AM");
  });

  it("the step clock's \"waiting on you · since …\" uses the same formatter", () => {
    expect(sinceWords(T, NOW, "America/Los_Angeles")).toBe("7:23 AM");
    const step = { status: "waiting", decision_id: "dec_1", decision_status: "pending", started_at: "2026-11-06T15:23:00Z" };
    // Before hydration (the server's paint) it is UTC, so the browser's first render matches it…
    expect(stepClock(step, NOW)?.text).toBe("waiting on you · since 3:23 PM");
    // …and after, the viewer's own zone (this test machine's).
    markHydrated();
    expect(viewerZone()).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
    expect(stepClock(step, NOW)?.text).toBe(`waiting on you · since ${sinceLocal(T, NOW, viewerZone())}`);
    expect(stepClock(step, NOW)?.kind === "waiting" && (stepClock(step, NOW) as { title: string }).title).toContain(stampLocal(T, viewerZone()));
  });
});
