// @vitest-environment happy-dom
/**
 * MINE — THE PERSONAL INBOX (BOSSAPP-875): ranked rows with the words of why, unread in bold,
 * Read / Snooze 1 day / Archive as the person's own presses, Mine ↔ Everyone.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InboxView } from "../app/projects/work-inbox";
import { parseInbox } from "../lib/inbox";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const FEED = { count: 2, unread: 1, snoozed: 1, archived: 0, items: [
  { item_key: "decision:d1", kind: "decision", ref_id: "d1", title: "Approve: Schema", work_item_id: "w1", for_you: true,
    risk_tier: 2, blocked_count: 2, score: 50, why: ["blocks 2", "for you"], read: false },
  { item_key: "work_item:w2", kind: "work_item", ref_id: "w2", title: "WI-2 Docs", work_item_id: "w2", for_you: true,
    risk_tier: 0, blocked_count: 0, score: 10, why: ["for you"], read: true },
] };

afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("the personal inbox", () => {
  it("parses defensively", () => {
    const f = parseInbox({ items: [{ item_key: "x", kind: "nope", why: ["a", 3] }] });
    expect(f.items[0].kind).toBe("decision");
    expect(f.items[0].why).toEqual(["a"]);
  });

  it("draws the ranked feed and sends the person's own presses", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify(method === "GET" ? FEED : { ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    await mount(<InboxView projectId="p1" />);
    expect($$('[data-testid^="ib-decision"], [data-testid^="ib-work_item"]')).toHaveLength(2);
    expect($('[data-testid="ib-decision:d1"]')?.textContent).toContain("blocks 2");
    expect($('[data-testid="ib-read-work_item:w2"]')).toBeNull();
    ($('[data-testid="ib-read-decision:d1"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url === "/v1/inbox/read")?.body).toMatchObject({ item_keys: ["decision:d1"], read: true });
    ($('[data-testid="ib-snooze-decision:d1"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url === "/v1/inbox/snooze")?.body).toMatchObject({ item_key: "decision:d1", minutes: 1440 });
    ($('[data-testid="ib-all"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.some((c) => c.method === "GET" && c.url.includes("mine=false"))).toBe(true);
  });
});
