// @vitest-environment happy-dom
/**
 * Export fetches the same filter href and downloads that body.
 * When x-truncated is 1, a paragraph says the cap hid older rows.
 * When the header is 0 or absent, the page does not mention a cap.
 * A sentence over six words is a paragraph.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToolCalls } from "../app/activity/tool-calls";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ROW = {
  id: "tca_1", project_id: "p1", run_id: "arun_1", agent: "atlas", principal: "usr_alice",
  server_id: "srv_gh", tool: "create_issue", args_sha256: "a".repeat(64),
  args_redacted: { title: "bug" }, decision: "allow", matched_policy_ids: [],
  decision_id: null, outcome: "succeeded", latency_ms: 42, recorded_at: "2026-11-12T10:00:00+00:00",
  screen: null, explanation: null, act_id: null, seq: null,
};

const BODY = "{\"id\":\"tca_1\"}\n";

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function installDownloadCapture(downloads: { href: string; download: string }[]) {
  const blobs: Blob[] = [];
  URL.createObjectURL = (blob: Blob) => {
    blobs.push(blob);
    return "blob:tool-export";
  };
  URL.revokeObjectURL = () => {};
  const real = HTMLElement.prototype.click;
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    if (this.getAttribute("data-testid") === "tc-export") {
      real.call(this);
      return;
    }
    downloads.push({ href: this.getAttribute("href") ?? "", download: this.download });
  });
  return blobs;
}

function serve(header: string | null) {
  const urls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const asked = String(url);
    urls.push(asked);
    if (asked.startsWith("/api/tool-calls/export")) {
      const headers = new Headers({
        "content-type": "application/x-ndjson",
        "content-disposition": "attachment; filename=\"boss-tool-calls.ndjson\"",
      });
      if (header !== null) headers.set("x-truncated", header);
      return new Response(BODY, { status: 200, headers });
    }
    return new Response(JSON.stringify({ items: [ROW], next: null }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }));
  return urls;
}

async function exportFiltered() {
  await mount(<ToolCalls projectId="p1" />);
  await settle();
  await click($("[data-testid=tc-chip-suspicious]"));
  await click($("[data-testid=tc-export]"));
  await settle();
}

describe("the tool-call export", () => {
  it("says the cap hid older rows when the export is truncated", async () => {
    const urls = serve("1");
    const downloads: { href: string; download: string }[] = [];
    const blobs = installDownloadCapture(downloads);
    await exportFiltered();

    const href = ($("[data-testid=tc-export]") as HTMLAnchorElement).getAttribute("href");
    expect(href).toBe("/api/tool-calls/export?project_id=p1&screen=suspicious");
    expect(urls.filter((u) => u.startsWith("/api/tool-calls/export"))).toEqual([href]);
    expect(downloads).toEqual([{ href: "blob:tool-export", download: "boss-tool-calls.ndjson" }]);
    expect(await blobs[0]?.text()).toBe(BODY);

    const note = $("[data-testid=tc-export-cap]");
    const words = (note?.textContent ?? "").trim().split(/\s+/).filter(Boolean);
    expect(note?.tagName).toBe("P");
    expect(words.length).toBeGreaterThan(6);
    expect(note?.textContent?.trim()).toBe("The cap hid older rows from this export.");
  });

  it.each([
    ["0", "0"],
    ["absent", null],
  ] as const)("does not mention a cap when x-truncated is %s", async (_label, header) => {
    const urls = serve(header);
    const downloads: { href: string; download: string }[] = [];
    const blobs = installDownloadCapture(downloads);
    await exportFiltered();

    const href = ($("[data-testid=tc-export]") as HTMLAnchorElement).getAttribute("href");
    expect(href).toBe("/api/tool-calls/export?project_id=p1&screen=suspicious");
    expect(urls.filter((u) => u.startsWith("/api/tool-calls/export"))).toEqual([href]);
    expect(downloads).toEqual([{ href: "blob:tool-export", download: "boss-tool-calls.ndjson" }]);
    expect(await blobs[0]?.text()).toBe(BODY);
    expect($("[data-testid=tc-export-cap]")).toBeNull();
    expect(document.body.innerHTML.toLowerCase()).not.toMatch(/\bcap\b/);
  });
});
