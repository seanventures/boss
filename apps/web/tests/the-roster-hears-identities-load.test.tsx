// @vitest-environment happy-dom
/**
 * A roster card drawn before the identities load shows the callsign once they do.
 *
 * Seen live on boss-1: 12 of 34 cards said "Program Manager", "COO", "CTO", "Deployment
 * Manager" instead of Rally, Cadence, Vector, Harbor — while `/v1/agents/identities` had
 * answered 200 with exactly those callsigns. The card resolved the identity at render and never
 * heard the store, so it kept the bundled cast's answer, and the bundled cast names a callsign
 * only for the hand-drawn roles. program_manager is one it does not.
 *
 * tests/identity-readers-subscribe.test.ts holds every other component to the same rule.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { V2Agent } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { RosterZones } = await import("../app/agents/roster-zones");
const { primeIdentities, resetIdentities } = await import("../lib/agent-identity");
const { CAST_BY_KEY } = await import("../lib/avatar-kit/cast");
const { resetLiveStores } = await import("../lib/agents-live");

const agent = (id: string, name: string): V2Agent => ({
  id, name, role: id, status: "IDLE", task: "No work in this window.", model: null,
  tokens: null, spend_usd: null, calls: 0, autonomy_level: 1, trust_score: 50, successful_runs: 3, violations: 0,
  hidden_failures: 0, last_seen_seconds: null, run_id: null, project: null, last_touched: null, registered: true,
} as V2Agent);

let root: Root | null = null;
let host: HTMLElement | null = null;
beforeEach(() => resetIdentities());
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  resetIdentities();
  resetLiveStores();
});

describe("the roster hears the identities load", () => {
  it("a card drawn before the load shows the callsign the API returned, not the role title", async () => {
    // The precondition that made the bug: the bundled cast has no callsign for this role.
    expect(CAST_BY_KEY.program_manager?.callsign).toBeUndefined();

    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(
      <RosterZones agents={[agent("program_manager", "Program Manager"), agent("coo", "COO")]} live={new Map()}
                   onOpen={() => {}} onAsk={() => {}} />,
    ));
    const name = (k: string) => host!.querySelector(`[data-testid="ag-name-${k}"]`)?.textContent;
    expect(name("program_manager")).toBe("Program Manager");

    await act(async () => primeIdentities([
      { role_key: "program_manager", display_name: "Program Manager", callsign: "Rally" },
      { role_key: "coo", display_name: "COO", callsign: "Cadence" },
    ]));

    expect(name("program_manager")).toBe("Rally");
    expect(name("coo")).toBe("Cadence");
    // The role still sits beside the callsign, as it does for every named agent.
    expect(host.querySelector('[data-testid="wf-agent-program_manager"] .ag-card-role')?.textContent).toBe("Program Manager");
  });
});
