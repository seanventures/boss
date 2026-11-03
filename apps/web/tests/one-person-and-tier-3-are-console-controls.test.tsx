/**
 * Two settings the API always took and the console never showed (boss-1, 2026-11-03: the operator
 * approved every tier-3 card twice and asked to hand approvals over; neither step was possible
 * from the screen):
 *   - Identity › Approval seats: "One person approves every tier" (single_operator_waiver).
 *   - Account › Approve on my behalf: "Also tier-3 decisions" (extra_risk_tiers: [3]).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const read = (p: string) => readFileSync(join(__dirname, "..", p), "utf8");

describe("console controls for handing approvals over", () => {
  it("the seats screen offers one person for every tier, through the policy write", () => {
    const src = read("app/identity/seats/seats-screen.tsx");
    expect(src).toContain('data-testid="seats-one-person-toggle"');
    expect(src).toMatch(/idPut\("\/v1\/identity\/policy", \{ single_operator_waiver: !on \}\)/);
    expect(src).toContain('permission="security_policy:manage"');
    expect(src).toContain("<OnePersonSwitch waiver={data.single_operator_waiver} />");
  });

  it("a new approval delegation can also take tier-3 decisions", () => {
    const src = read("app/account/agents/approve-on-my-behalf.tsx");
    expect(src).toContain('data-testid="tier-3"');
    expect(src).toContain("...(tier3 ? { extra_risk_tiers: [3] } : {}),");
  });
});
