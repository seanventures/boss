import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Infra offers a host action only when BOSS can carry it out.
 *
 * Console walk on boss-1, 2026-10-02: the hosts panel said "BOSS is configured not to reach out
 * over SSH, so no host can be verified, provisioned or deployed to" and, underneath, still offered
 * Register a host, Verify and Re-verify — each of which ends at that refusal, after a key may
 * have been minted and pasted onto a real machine. Only Retire was gated.
 *
 * Enumerated: every call site of each host action in the panel must sit behind its gate.
 */
const SRC = codeOnly("app/delivery-manage.tsx");

const GATES: Record<string, string> = {
  verifyDeploymentHostAction: "canReachHosts ?",
  registerDeploymentHostAction: "openReg && canReachHosts ?",
  installHostKeyAction: "hostWritesAllowed ?",
  provisionHostAction: "hostWritesAllowed ?",
};

function callSites(action: string): number[] {
  const out: number[] = [];
  let at = SRC.indexOf(`callAction(${action})(`);
  while (at >= 0) {
    out.push(at);
    at = SRC.indexOf(`callAction(${action})(`, at + 1);
  }
  return out;
}

describe("host actions in Infra", () => {
  it("finds every call site", () => {
    expect(callSites("verifyDeploymentHostAction").length).toBeGreaterThanOrEqual(4);
    expect(callSites("registerDeploymentHostAction").length).toBe(1);
  });

  for (const [action, gate] of Object.entries(GATES)) {
    it(`${action} is offered only behind ${gate}`, () => {
      for (const at of callSites(action)) {
        const before = SRC.slice(Math.max(0, at - 1200), at);
        expect(before.lastIndexOf(gate), `${action} at ${at} is not behind ${gate}`)
          .toBeGreaterThan(-1);
      }
    });
  }

  it("the gate is the same precondition the notice at the top reports", () => {
    expect(SRC).toContain("const canReachHosts = blocker === null;");
    expect(SRC).toContain('data-testid="fleet-action-unavailable"');
  });
});
