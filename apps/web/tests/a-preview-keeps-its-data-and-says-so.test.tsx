// @vitest-environment happy-dom
/**
 * recipe-box, 2026-11-18: a preview stopped by BOSS after 20 idle minutes came back with an
 * empty database, and its row still said "Expires 11:42 AM" while Not running. An app's preview
 * data is now kept across stops and restarts (engine preview_data.py); the row says so, offers
 * Fresh data to start it empty, and shows an expiry only while the preview runs.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseCockpit, type Cockpit } from "../lib/cockpit";
import { $, click, mount, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit, cockpitPrOpen } from "./_helpers/factory-fixtures";

const forms: FormData[] = [];
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  startPreviewAction: (_prev: unknown, form: FormData) => { forms.push(form); return new Promise(() => {}); },
}));
vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Outputs, PREVIEW_DATA_TIP } = await import("../app/factory/outputs");

afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.restoreAllMocks(); forms.length = 0; });

function view(c: Cockpit) {
  return <Outputs projectId="p1" actor="ceo" cockpit={c} open="preview" onOpen={() => {}} onLogs={() => {}} onRefresh={() => {}} />;
}
function withPreview(preview: Record<string, unknown>): Cockpit {
  const base = cockpitPrOpen();
  return parseCockpit({ ...base, outputs: { ...(base.outputs as Record<string, unknown>), preview } });
}

describe("a preview's data", () => {
  it("a stopped preview shows no expiry; a running one does", async () => {
    stubFetch(cockpit());
    await mount(view(withPreview({ id: "prev_1", status: "stopped", word: "Not running",
      expires_at: "2026-11-18T11:42:00Z", error: "Stopped by BOSS: nobody opened it for 20 min." })));
    expect($('[data-testid="fx-preview-expires"]')).toBeNull();
    unmount();
    await mount(view(withPreview({ id: "prev_1", status: "running", word: "Live",
      expires_at: "2026-11-18T11:42:00Z" })));
    expect($('[data-testid="fx-preview-expires"]')?.textContent).toMatch(/^Expires /);
  });

  it("says the data is kept, and Fresh data restarts it empty after a confirm", async () => {
    stubFetch(cockpit());
    await mount(view(withPreview({ id: "prev_1", status: "stopped", word: "Not running" })));
    const note = $('[data-testid="fx-preview-data"]');
    expect(note?.textContent).toBe("Data kept");
    expect(note?.getAttribute("title")).toBe(PREVIEW_DATA_TIP);
    expect(PREVIEW_DATA_TIP).toMatch(/kept when the preview stops/);

    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await click($('[data-testid="fx-preview-fresh"]'));
    expect(forms).toHaveLength(0);

    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await click($('[data-testid="fx-preview-fresh"]'));
    expect(forms).toHaveLength(1);
    expect(forms[0].get("fresh_data")).toBe("true");
  });

  it("a plain Restart keeps the data", async () => {
    stubFetch(cockpit());
    await mount(view(withPreview({ id: "prev_1", status: "stopped", word: "Not running" })));
    await click($('[data-testid="fx-preview-restart"]'));
    expect(forms).toHaveLength(1);
    expect(forms[0].get("fresh_data")).toBeNull();
  });
});
