// @vitest-environment happy-dom
/**
 * The Preview tab prints the preview process stdout.
 * A person can read that this is that log, not the preview's requests or jobs.
 * A sentence over six words is a paragraph.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, mount, unmount } from "./_helpers/live-render";

vi.mock("../app/factory/cockpit-actions", () => ({ fetchBuildLog: async () => null }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => null }));
vi.mock("../app/factory/cockpit-util", () => ({
  areaHref: () => "#",
  clockOf: () => "",
  whoName: (name: string) => name,
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the preview log", () => {
  it("says the text is the process log, not requests or jobs", async () => {
    vi.stubGlobal("EventSource", class {
      static CLOSED = 2;
      addEventListener() {}
      close() {}
    });
    const { LogsDrawer } = await import("../app/factory/logs-drawer");
    await mount(<LogsDrawer projectId="p1" runId="crun_pr" previewId="prev_1" deployId={null} tab="preview" onTab={() => {}} onClose={() => {}} />);
    const kind = $("[data-testid=fx-preview-log-kind]");
    const words = (kind?.textContent ?? "").trim().split(/\s+/).filter(Boolean);
    expect(kind?.tagName).toBe("P");
    expect(words.length).toBeGreaterThan(6);
    expect(kind?.textContent?.trim()).toBe("The text is the preview process log, not its requests or jobs.");
  });
});
