// @vitest-environment happy-dom
/**
 * A BUILD STARTED FROM A TICKET WEARS ITS TICKET (BOSSAPP-1047 Phase C): the cockpit's read
 * (GET /v1/code-tasks/{id}/cockpit) carries `ticket`, and the header shows the same chip the Work
 * tab's rows do, linking to the ticket, with what BOSS last wrote back. A build from no ticket
 * shows none.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseCockpit } from "../lib/cockpit";
import { $, mount, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit } from "./_helpers/factory-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { Cockpit } = await import("../app/factory/cockpit");

const TICKET = { provider: "jira", key: "LEND-142", url: "https://np.atlassian.net/browse/LEND-142", connection_id: "tc_1",
                 status: "In Progress", status_category: "doing", sync_status: "synced", work_item_id: "wi_1" };

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the build cockpit's ticket badge", () => {
  it("parses the ticket, and nothing for a build from no ticket", () => {
    expect(parseCockpit(cockpit({ ticket: TICKET })).ticket).toEqual({
      provider: "jira", key: "LEND-142", url: "https://np.atlassian.net/browse/LEND-142", status: "In Progress",
      syncStatus: "synced", workItemId: "wi_1" });
    expect(parseCockpit(cockpit()).ticket).toBeNull();
    expect(parseCockpit(cockpit({ ticket: null })).ticket).toBeNull();
  });

  it("shows the chip linking to the ticket, red when the write-back failed", async () => {
    stubFetch(cockpit({ ticket: TICKET }));
    await mount(<Cockpit projectId="boss-dogfood" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    const chip = $('[data-testid="fx-ticket"]');
    expect(chip?.getAttribute("href")).toBe("https://np.atlassian.net/browse/LEND-142");
    expect(chip?.textContent).toContain("LEND-142");
    expect(chip?.textContent).toContain("Jira: In Progress ✓");
    unmount();
    stubFetch(cockpit({ ticket: { ...TICKET, sync_status: "failed" } }));
    await mount(<Cockpit projectId="boss-dogfood" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    expect($('[data-testid="fx-ticket"] .tk-chip-sync')?.getAttribute("data-tone")).toBe("red");
    expect($('[data-testid="fx-ticket"]')?.textContent).toContain("write-back failed");
  });

  it("a build from no ticket has no badge", async () => {
    stubFetch(cockpit());
    await mount(<Cockpit projectId="boss-dogfood" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    expect($('[data-testid="fx-ticket"]')).toBeNull();
  });
});
