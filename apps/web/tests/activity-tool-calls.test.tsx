// @vitest-environment happy-dom
/**
 * ACTIVITY › TOOL CALLS (BOSSAPP-819): the Activity area's second tab reads the tool-call audit
 * (GET /v1/tool-calls through /api/tool-calls). A chip filters, a cell's + filters for that
 * tool, "Older" follows the keyset cursor, Export carries the filter, and a row opens a drawer
 * with the redacted arguments, the decision link, the screen verdict and the policy.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActivityTabs, ToolCalls } from "../app/activity/tool-calls";
import { mapToolCallPage } from "../lib/tool-calls";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const ROW = {
  id: "tca_1", project_id: "p1", run_id: "arun_1", agent: "atlas", principal: "usr_alice",
  server_id: "srv_gh", tool: "create_issue", args_sha256: "a".repeat(64),
  args_redacted: { title: "bug", token: "[redacted]" }, decision: "ask", matched_policy_ids: ["ask"],
  decision_id: "dec_7", outcome: "succeeded", latency_ms: 42, recorded_at: "2026-11-12T10:00:00+00:00",
  screen: { mode: "patterns", verdict: "suspicious", rules: ["ignore_previous"] },
  explanation: "tier 2 asks a person", act_id: null, seq: null,
};
const OLDER = { ...ROW, id: "tca_0", tool: "read_file", decision: "allow", decision_id: null, screen: null,
                outcome: "refused", explanation: null, recorded_at: "2026-11-12T09:00:00+00:00" };

afterEach(() => { unmount(); vi.restoreAllMocks(); });

function serve(urls: string[]) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    urls.push(String(url));
    const q = new URL(String(url), "http://x").searchParams;
    const body = q.get("cursor") === "c1" ? { items: [OLDER], next: null }
      : q.get("screen") === "suspicious" || q.get("tool") ? { items: [ROW], next: null }
      : { items: [ROW], next: "c1" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

describe("Activity › Tool calls", () => {
  it("maps the API's page", () => {
    const page = mapToolCallPage({ items: [ROW, { nope: true }], next: "c1" });
    expect(page.items).toHaveLength(1);
    expect(page.items[0]).toMatchObject({ tool: "create_issue", decisionId: "dec_7", explanation: "tier 2 asks a person" });
    expect(page.next).toBe("c1");
  });

  it("lists, filters, pages and opens one call", async () => {
    const urls: string[] = [];
    serve(urls);
    await mount(<ToolCalls projectId="p1" />);
    await settle();
    expect(urls[0]).toBe("/api/tool-calls?project_id=p1");
    expect($$('[data-testid="tc-row"]')).toHaveLength(1);
    expect(($('[data-testid="tc-export"]') as HTMLAnchorElement).getAttribute("href"))
      .toBe("/api/tool-calls/export?project_id=p1");

    ($('[data-testid="tc-more"]') as HTMLButtonElement).click();
    await settle();
    expect(urls.at(-1)).toContain("cursor=c1");
    expect($$('[data-testid="tc-row"]')).toHaveLength(2);

    ($('[data-testid="tc-chip-suspicious"]') as HTMLButtonElement).click();
    await settle();
    expect(urls.at(-1)).toBe("/api/tool-calls?project_id=p1&screen=suspicious");
    expect(($('[data-testid="tc-export"]') as HTMLAnchorElement).getAttribute("href")).toContain("screen=suspicious");
    expect($$('[data-testid="tc-row"]')).toHaveLength(1);

    ($('[data-testid="tc-row"] .tc-plus') as HTMLButtonElement).click();
    await settle();
    expect(urls.at(-1)).toContain("tool=create_issue");
    expect($('[data-testid="tc-token-tool"]')).not.toBeNull();

    ($('[data-testid="tc-row"]') as HTMLElement).click();
    await settle();
    const drawer = $('[data-testid="tc-drawer"]')!;
    expect(drawer).not.toBeNull();
    expect(drawer.querySelector('[data-testid="tc-args"] pre')?.textContent).toContain('"token": "[redacted]"');
    expect($('[data-testid="tc-sha"]')?.textContent).toBe(`sha256 ${"a".repeat(64)}`);
    expect(($('[data-testid="tc-decision-link"]') as HTMLAnchorElement).getAttribute("href")).toContain("decision=dec_7");
    expect($('[data-testid="tc-screen"]')?.textContent).toContain("suspicious");
    expect($('[data-testid="tc-screen"]')?.textContent).toContain("ignore_previous");
    expect($('[data-testid="tc-explanation"]')?.textContent).toBe("tier 2 asks a person");
    ($('[data-testid="tc-drawer-close"]') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="tc-drawer"]')).toBeNull();
  });

  it("is a tab beside the log", async () => {
    serve([]);
    await mount(<ActivityTabs projectId="p1" log={<div data-testid="the-log">log</div>} />);
    expect($('[data-testid="the-log"]')).not.toBeNull();
    ($('[data-testid="activity-tab-tools"]') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="the-log"]')).toBeNull();
    expect($('[data-testid="tool-calls"]')).not.toBeNull();
    expect(window.location.search).toContain("activity_tab=tools");
    ($('[data-testid="activity-tab-log"]') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="the-log"]')).not.toBeNull();
  });
});
