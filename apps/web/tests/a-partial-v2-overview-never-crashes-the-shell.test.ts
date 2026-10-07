/**
 * A role that may not read every section of the v2 overview (the seeded `developer`, no budget
 * read) got it without `burn`, and every v2 screen crashed on overview.burn.spent_usd (round 3
 * role walk). overviewFrom fills a missing section with the empty one.
 */
import { describe, expect, it } from "vitest";

import { overviewFrom } from "../lib/v2/overview";

describe("overviewFrom", () => {
  it("fills what the API left out", () => {
    const o = overviewFrom("p1", { company: { id: "p1", name: "Acme" }, gates: 2, projects: [] });
    expect(o.burn).toEqual({ spent_usd: 0, cap_usd: null });
    expect(o.autonomy.mode).toBe("Supervised");
    expect(o.company.name).toBe("Acme");
    expect(o.gates).toBe(2);
    expect(o.apiReachable).toBe(true);
  });
  it("keeps what it sent", () => {
    const o = overviewFrom("p1", { burn: { spent_usd: 12, cap_usd: 50 } });
    expect(o.burn).toEqual({ spent_usd: 12, cap_usd: 50 });
  });
});
