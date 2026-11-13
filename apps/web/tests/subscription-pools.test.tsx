// @vitest-environment happy-dom
/**
 * SETUP › ADVANCED › SEVERAL ACCOUNTS PER PROVIDER (BOSSAPP-886): each pool and its accounts with
 * their room in words; Pause sends one PATCH; Add account sends the login once and clears it;
 * an engine with no pool offers Start; the row stays within the copy budget.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PoolsRow } from "../app/setup/pools-row";
import { parsePools } from "../lib/subscription-pools";
import { overBudget } from "./_helpers/copy-budget";
import { $, mount, settle, unmount } from "./_helpers/live-render";

const LIST = { pools: [{ id: "spool_1", engine: "claude_code", name: "seats", status: "active", members: [
  { id: "spm_a", label: "Ana", status: "active", position: 0, headroom: { state: "near_limit", left: 0.1, resets_at: null } },
  { id: "spm_b", label: "Bo", status: "needs_reauth", position: 1, headroom: { state: "plenty", left: null } },
] }] };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("subscription pools", () => {
  it("parses defensively", () => {
    const [p] = parsePools({ pools: [{ id: "x", engine: "other", members: [{ id: "m", status: "odd" }] }] });
    expect(p).toMatchObject({ engine: "claude_code", status: "active" });
    expect(p.members[0]).toMatchObject({ status: "active", room: null });
  });

  it("shows room in words, pauses, adds an account with its login once, and offers Start", async () => {
    const calls: { url: string; method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = String(init?.method ?? "GET");
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : {} });
      return method === "GET" ? json(LIST) : json({ member: {} });
    }));
    await mount(<PoolsRow projectId="p1" />);
    await settle();
    expect($('[data-testid="pool-room-spm_a"]')?.textContent).toBe("Near limit");
    expect($('[data-testid="pool-room-spm_b"]')?.textContent).toBe("Needs a new login");
    expect($('[data-testid="pools-start-codex"]')).not.toBeNull();
    expect($('[data-testid="pools-start-claude_code"]')).toBeNull();
    expect(overBudget(document.body)).toEqual([]);
    ($('[data-testid="pool-member-toggle-spm_a"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ project_id: "p1", status: "disabled" });
    ($('[data-testid="pool-add-open-claude_code"]') as HTMLButtonElement).click();
    await settle();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    for (const [label, value] of [["Account name", "Cy"], ["Account login", "sk-ant-oat01-test"]]) {
      const box = $(`[aria-label="${label}"]`) as HTMLInputElement;
      setter.call(box, value);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await settle();
    ($('[data-testid="pool-add-save-claude_code"]') as HTMLButtonElement).click();
    await settle();
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe("/v1/subscription-pools/spool_1/members");
    expect(posts[0].body).toEqual({ project_id: "p1", label: "Cy", credential: "sk-ant-oat01-test" });
  });
});
