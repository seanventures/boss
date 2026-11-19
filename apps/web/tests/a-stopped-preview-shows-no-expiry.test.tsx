// @vitest-environment happy-dom
/**
 * recipe-box, 2026-11-18: a preview row said "Expires 11:42 AM" while Not running. An expiry
 * is shown only while the preview runs.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseCockpit, type Cockpit } from "../lib/cockpit";
import { $, mount, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit, cockpitPrOpen } from "./_helpers/factory-fixtures";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Outputs } = await import("../app/factory/outputs");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

function view(preview: Record<string, unknown>) {
  const base = cockpitPrOpen();
  const c: Cockpit = parseCockpit({ ...base, outputs: { ...(base.outputs as Record<string, unknown>), preview } });
  return <Outputs projectId="p1" actor="ceo" cockpit={c} open="preview" onOpen={() => {}} onLogs={() => {}} onRefresh={() => {}} />;
}

describe("a preview's expiry", () => {
  it("is not shown on a stopped preview, and is on a running one", async () => {
    stubFetch(cockpit());
    await mount(view({ id: "prev_1", status: "stopped", word: "Not running", expires_at: "2026-11-18T11:42:00Z",
                       error: "Stopped by BOSS: nobody opened it for 20 min." }));
    expect($('[data-testid="fx-preview-expires"]')).toBeNull();
    unmount();
    await mount(view({ id: "prev_1", status: "running", word: "Live", expires_at: "2026-11-18T11:42:00Z" }));
    expect($('[data-testid="fx-preview-expires"]')?.textContent).toMatch(/^Expires /);
  });
});
