// @vitest-environment happy-dom
/**
 * Infra offers a host action only when BOSS can carry it out.
 *
 * Console walk on boss-1, 2026-10-02: the hosts panel said "BOSS is configured not to reach out
 * over SSH, so no host can be verified, provisioned or deployed to" and, underneath, still offered
 * Register a host, Verify and Re-verify — each of which ends at that refusal, after a key may
 * have been minted and pasted onto a real machine. Only Retire was gated.
 *
 * Rewritten for the Fleet (spec §4.3-§4.4, §9.2): the gate is the server's (`preconditions` on
 * the fleet, `can` on the host), and every place an action can appear — the rack's inline stage
 * action, the host drawer's SETUP and CHECKS, `+ Add › Host` — is walked with SSH out on and off.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { byTestId, click, mountFleet, unmountInfra } from "./_helpers/infra-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));

afterEach(() => unmountInfra());

/** Every control that reaches the machine (SSH) or writes to it. */
const REACHES = ["inf-stage-ran", "inf-stage-verify", "inf-stage-verify-provision", "inf-stage-install-key", "inf-stage-docker"];

describe("host actions in Infra", () => {
  it("with SSH out allowed, every stage control is offered", async () => {
    await mountFleet("view=fleet&host=h_setup");
    for (const id of REACHES) expect(byTestId(id), id).not.toBeNull();
    expect(byTestId("inf-stage-generate")).not.toBeNull();
    expect(byTestId("inf-ssh-off")).toBeNull();
  });

  it("with SSH out off, nothing that reaches a machine is drawn — and the line says why", async () => {
    await mountFleet("view=fleet&host=h_setup", { variant: "blocked" });
    for (const id of REACHES) expect(byTestId(id), id).toBeNull();
    // Minting or adopting a key touches nothing on the machine: still offered.
    expect(byTestId("inf-stage-generate")).not.toBeNull();
    expect(byTestId("inf-stage-adopt")).not.toBeNull();
    expect(byTestId("inf-ssh-off")?.getAttribute("title")).toContain("BOSS_SSH_OUTWARD_ALLOWED");
    // The rack's inline Verify, and Add host, are gone too.
    expect(byTestId("inf-rack-stage-verify")).toBeNull();
    await click("infra-add");
    expect(byTestId("infra-add-host")).toBeNull();
  });

  it("Re-verify on a ready machine follows the same gate", async () => {
    await mountFleet("view=fleet&host=h_ready");
    expect(byTestId("inf-host-reverify")).not.toBeNull();
    unmountInfra();
    await mountFleet("view=fleet&host=h_ready", { variant: "blocked" });
    expect(byTestId("inf-host-checks")).not.toBeNull();
    expect(byTestId("inf-host-reverify")).toBeNull();
  });
});
