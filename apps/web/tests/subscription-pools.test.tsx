// @vitest-environment happy-dom
/**
 * SETUP › ADVANCED › SEVERAL ACCOUNTS PER PROVIDER (BOSSAPP-886): each pool and its accounts with
 * their room in words; Pause sends one PATCH; Add account sends the login once and clears it;
 * an engine with no pool offers Start; the row stays within the copy budget.
 *
 * BOSSAPP-1042: a pool can be paused and removed — an empty one after a confirm (one DELETE), one
 * with accounts is refused in words with no call (the API refuses it too, so no stored login is
 * orphaned); the credential field is labelled for what it is (Claude: "Long-lived token", Codex:
 * "auth.json"), visibly and for assistive tech alike.
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
    for (const [label, value] of [["Account name", "Cy"], ["Long-lived token", "sk-ant-oat01-test"]]) {
      const box = $(`[aria-label="${label}"]`) as HTMLInputElement;
      setter.call(box, value);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await settle();
    ($('[data-testid="pool-add-save-claude_code"]') as HTMLButtonElement).click();
    await settle();
    const posts = calls.filter((c) => c.method === "POST");
    expect(document.body.textContent).not.toMatch(/\bLogin\b/);
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe("/v1/subscription-pools/spool_1/members");
    expect(posts[0].body).toEqual({ project_id: "p1", label: "Cy", credential: "sk-ant-oat01-test" });
  });

  function stub(list: unknown) {
    const calls: { url: string; method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = String(init?.method ?? "GET");
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : {} });
      return method === "GET" ? json(list) : json({ deleted: "spool_1" });
    }));
    return calls;
  }
  const press = async (sel: string) => { ($(sel) as HTMLButtonElement).click(); await settle(); };

  it("a pool with accounts is refused in words and nothing is sent", async () => {
    const calls = stub(LIST);
    await mount(<PoolsRow projectId="p1" />);
    await press('[data-testid="pool-remove-claude_code"]');
    expect($('[data-testid="pools-said"]')?.textContent).toBe(
      "Remove its 2 accounts first.");
    expect($('[data-testid="pool-remove-confirm-claude_code"]')).toBeNull();
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
  });

  it("an empty pool is removed after a confirm, with one DELETE; Keep sends nothing", async () => {
    const calls = stub({ pools: [{ ...LIST.pools[0], members: [] }] });
    await mount(<PoolsRow projectId="p1" />);
    await press('[data-testid="pool-remove-claude_code"]');
    expect($('[data-testid="pool-remove-confirm-claude_code"]')?.getAttribute("role")).toBe("alertdialog");
    await press('[data-testid="pool-remove-confirm-claude_code"] button.su-link');
    expect($('[data-testid="pool-remove-confirm-claude_code"]')).toBeNull();
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
    await press('[data-testid="pool-remove-claude_code"]');
    await press('[data-testid="pool-remove-yes-claude_code"]');
    const dels = calls.filter((c) => c.method === "DELETE");
    expect(dels.map((c) => c.url)).toEqual(["/v1/subscription-pools/spool_1?project_id=p1"]);
    expect($('[data-testid="pools-said"]')?.textContent).toBe("Claude pool removed.");
  });

  it("pauses the whole pool with one PATCH", async () => {
    const calls = stub(LIST);
    await mount(<PoolsRow projectId="p1" />);
    await press('[data-testid="pool-toggle-claude_code"]');
    const patch = calls.find((c) => c.method === "PATCH");
    expect(patch?.url).toBe("/v1/subscription-pools/spool_1");
    expect(patch?.body).toEqual({ project_id: "p1", status: "disabled" });
  });

  it("names the credential for what it is, per engine", async () => {
    stub({ pools: [LIST.pools[0], { id: "spool_2", engine: "codex", name: "cx", status: "active", members: [] }] });
    await mount(<PoolsRow projectId="p1" />);
    await press('[data-testid="pool-add-open-claude_code"]');
    await press('[data-testid="pool-add-open-codex"]');
    const claude = $('[data-testid="pool-add-claude_code"]')!;
    const codex = $('[data-testid="pool-add-codex"]')!;
    expect(claude.textContent).toContain("Long-lived token");
    expect(claude.querySelector('input[type="password"]')?.getAttribute("aria-label")).toBe("Long-lived token");
    expect(codex.textContent).toContain("auth.json");
    expect(codex.querySelector('input[type="password"]')?.getAttribute("aria-label")).toBe("auth.json");
  });
});
