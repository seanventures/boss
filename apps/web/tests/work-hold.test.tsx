// @vitest-environment happy-dom
/**
 * PAUSE ALL / RESTORE (BOSSAPP-878): the preview is a dry run said in few words, Confirm sends
 * the real hold; a held item says so and Restore previews before it puts the tree back.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ItemHold } from "../app/projects/work-hold";
import { parseNode, planWords } from "../lib/work-items";
import { $, mount, settle, unmount } from "./_helpers/live-render";

afterEach(() => { unmount(); vi.restoreAllMocks(); });

const PLAN = { hold: { dry_run: true, members: [
  { member_kind: "work_item", member_id: "w1", action: "held" },
  { member_kind: "work_item", member_id: "w2", action: "held" },
  { member_kind: "workflow_run", member_id: "r1", action: "paused" },
  { member_kind: "workflow_run", member_id: "r2", action: "already_paused" },
] } };

function serve(calls: { url: string; body: Record<string, unknown> }[], reply: unknown) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : {} });
    return new Response(JSON.stringify(reply), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

describe("holding a work tree", () => {
  it("says a plan in few words", () => {
    expect(planWords(PLAN.hold.members)).toBe("pauses 1 run · holds 2 items");
    expect(planWords([{ member_kind: "workflow_run", member_id: "r1", restored_action: "resumed" },
                      { member_kind: "workflow_run", member_id: "r2", restored_action: "left_paused" }], true))
      .toBe("resumes 1 run · leaves paused 1 run");
    expect(planWords([])).toBe("nothing to change");
  });

  it("previews Pause all, then confirms it", async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    serve(calls, PLAN);
    const item = parseNode({ id: "w1", title: "Epic", status: "active", children: [], hold: null });
    await mount(<ItemHold projectId="p1" item={item} demo={false} onChanged={() => {}} />);
    ($('[data-testid="wt-pause-all"]') as HTMLButtonElement).click();
    await settle();
    expect(calls[0].body).toMatchObject({ root_kind: "work_item", root_id: "w1", dry_run: true });
    expect($('[data-testid="wt-hold-plan"]')?.textContent).toBe("Pause all pauses 1 run · holds 2 items.");
    ($('[data-testid="wt-hold-confirm"]') as HTMLButtonElement).click();
    await settle();
    expect(calls[1].body).toMatchObject({ root_id: "w1", dry_run: false });
  });

  it("shows a held item and restores it", async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    serve(calls, { hold: { members: [] } });
    const item = parseNode({ id: "w1", title: "Epic", status: "active", children: [],
      hold: { id: "hold_1", root_kind: "work_item", root_id: "w1", mode: "pause", status: "active", reason: "spend spike", created_by: "person:ceo" } });
    await mount(<ItemHold projectId="p1" item={item} demo={false} onChanged={() => {}} />);
    expect($('[data-testid="wt-hold-banner"]')?.textContent).toContain("spend spike");
    ($('[data-testid="wt-restore"]') as HTMLButtonElement).click();
    await settle();
    expect(calls[0].url).toBe("/v1/work-holds/hold_1/restore");
    expect(calls[0].body).toMatchObject({ dry_run: true });
  });
});
