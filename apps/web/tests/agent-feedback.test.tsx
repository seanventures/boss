// @vitest-environment happy-dom
/**
 * ACTIVITY › FEEDBACK (BOSSAPP-889): what agents reported with report_friction, one card per
 * target as the API groups them; Open / All; a person's Resolve sends exactly one PATCH.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentFeedback } from "../app/activity/agent-feedback";
import { ActivityTabs } from "../app/activity/tool-calls";
import { parseGroups } from "../lib/agent-feedback";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const ITEM = { id: "afb_1", role_key: "forge", run_ref: "crun_1", kind: "complaint", target_kind: "tool",
               target: "read_logs", body: "read_logs returned nothing", evidence: ["read_logs -> []"],
               status: "open", created_at: "2026-11-13T10:00:00+00:00" };
const BODY = { groups: [
  { target_kind: "tool", target: "read_logs", complaints: 1, suggestions: 1, open: 2, roles: ["forge", "harbor"],
    items: [ITEM, { ...ITEM, id: "afb_2", kind: "suggestion", role_key: "harbor", body: "add a since filter" }] },
  { target_kind: "grant", target: "github.write", complaints: 1, suggestions: 0, open: 1, roles: ["forge"],
    items: [{ ...ITEM, id: "afb_3", target_kind: "grant", target: "github.write", body: "could not push" }] },
] };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("agent feedback", () => {
  it("parses groups defensively", () => {
    const g = parseGroups({ groups: [{ target: "x", items: [{ id: "a", status: "weird", kind: "nope" }] }] });
    expect(g[0].target_kind).toBe("other");
    expect(g[0].items[0]).toMatchObject({ status: "open", kind: "complaint", evidence: [] });
    expect(parseGroups(null)).toEqual([]);
  });

  it("lists one card per target, switches Open/All and resolves with one PATCH", async () => {
    const calls: { url: string; method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = String(init?.method ?? "GET");
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : {} });
      return method === "GET" ? json(BODY) : json({ feedback: { ...ITEM, status: "resolved" } });
    }));
    await mount(<AgentFeedback projectId="p1" />);
    await settle();
    expect(calls[0].url).toBe("/v1/agent-feedback?project_id=p1&status=open");
    expect($$('[data-testid^="agent-feedback-group-"]').map((e) => e.getAttribute("data-testid")))
      .toEqual(["agent-feedback-group-tool-read_logs", "agent-feedback-group-grant-github.write"]);
    expect($('[data-testid="agent-feedback-afb_1"]')?.textContent).toContain("forge in crun_1");
    ($('[data-testid="agent-feedback-show-all"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.some((c) => c.url === "/v1/agent-feedback?project_id=p1")).toBe(true);
    ($('[data-testid="agent-feedback-resolve-afb_3"]') as HTMLButtonElement).click();
    await settle();
    const patches = calls.filter((c) => c.method === "PATCH");
    expect(patches).toHaveLength(1);
    expect(patches[0].url).toContain("/v1/agent-feedback/afb_3");
    expect(patches[0].body).toEqual({ project_id: "p1", status: "resolved" });
    expect($('[data-testid="agent-feedback-said"]')?.textContent).toBe("Resolved.");
  });

  it("is Activity's third tab and keeps its place in the URL", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ groups: [] })));
    await mount(<ActivityTabs projectId="p1" log={<p data-testid="the-log">log</p>} />);
    ($('[data-testid="activity-tab-feedback"]') as HTMLButtonElement).click();
    await settle();
    expect(window.location.search).toContain("activity_tab=feedback");
    expect($('[data-testid="agent-feedback-empty"]')).not.toBeNull();
    ($('[data-testid="activity-tab-log"]') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="the-log"]')).not.toBeNull();
  });
});
