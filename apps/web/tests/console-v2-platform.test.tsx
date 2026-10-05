import { describe, expect, it } from "vitest";

import { parseFleet, parseRack } from "../lib/infra-view";
import { codeOnly } from "./_helpers/source";

/**
 * Infra & Models. (The v2 Workflows screen is retired: /v2/boss opens the console Library —
 * tests/legacy-areas.test.ts.)
 *
 * The v2 Infra screen (components/v2/InfraScreen.tsx) retired: /v2/infra redirects to Infra
 * (parity P-S-51). Its rendering is held by the new screens' own tests:
 *   - an unready host / no hosts           infra-fleet-draws-racks-from-the-view ("a machine being
 *                                          set up shows the Stage line…", "draws This computer
 *                                          first…") and infra-offers-only-host-actions-that-can-work
 *   - model spend, estimated prices        models-tab-matches-the-fixture-numbers ("$2.41 est.")
 *   - an integration's risk tier/boundary  Skills & MCP (app/skills/mcp-drawer.tsx;
 *                                          skills-and-mcp-are-managed-in-one-place)
 *   - deployed apps: open app, last deploy infra-deploys-a-repository-to-a-vm-through-an-approval
 *     and why it failed, never deployed,   ("a live deploy's drawer: … Open app", "a failed deploy's
 *     no machine of ours, retired          drawer says why…"), infra-fleet-draws-racks-from-the-view
 *                                          ("NOT PLACED … Never deployed", "retired hidden")
 *
 * One promise is about what is NOT drawn, which no render test of a fixture can catch, so it
 * stays here, against the parser that is the only reader of the wire.
 */
describe("Infra never shows a fleet load", () => {
  it("drops any utilisation the wire carries — BOSS records verification, not load", () => {
    // A fabricated 62% is indistinguishable from a real one. This console has already shipped
    // one number that was quietly made up.
    const rack = parseRack({
      id: "h1", name: "reference host", address: "10.0.0.1", arch: "arm64",
      load: 0.62, cpu_percent: 62, memory_percent: 40, utilisation: 0.5,
    });
    expect(Object.keys(rack).filter((k) => /load|cpu|mem|util/i.test(k))).toEqual([]);
    const fleet = parseFleet({ racks: [{ id: "h1", load: 0.62 }], load: 0.62 });
    expect(JSON.stringify(fleet)).not.toMatch(/0\.62|"load"/);
  });

  it("and the rack draws no percentage of load", () => {
    for (const f of ["app/infra/rack.tsx", "app/infra/fleet.tsx", "app/infra/host-drawer.tsx"]) {
      // Code only: rack.tsx's header says "No CPU, no memory" — which is the point.
      expect(codeOnly(f), f).not.toMatch(/% load|cpu|utilisation|utilization/i);
    }
  });
});
