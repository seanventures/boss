// @vitest-environment happy-dom
/**
 * S4 · The one card for an irreversible deletion (BOSSAPP-1156): exactly what goes and what
 * stays, the GB, the typed tool it will run, and the rule proposal in one tick box. One tap:
 * Delete N · free X GB binds to the dry run's plan hash and carries make_rule; the agent's
 * alternative ("Keep 20 instead") re-plans; Not now keeps work paused and asks again. Drawn
 * wherever decisions are drawn, from the decision's own `recommendation`.
 */
import { join } from "node:path";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";

import * as storage from "../lib/infra-storage";
import { demoDecision } from "../lib/infra-storage-demo";
import { codeOnly } from "./_helpers/source";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";

vi.mock("../app/client-forms", () => ({ showToast: vi.fn() }));

let answered: MockInstance<typeof storage.answerStorageDelete>;
let replanned: MockInstance<typeof storage.replanStorageStep>;
beforeEach(() => {
  answered = vi.spyOn(storage, "answerStorageDelete").mockResolvedValue({ ok: true, status: 200, body: {}, error: null });
  replanned = vi.spyOn(storage, "replanStorageStep").mockResolvedValue({ ok: true, status: 200, body: {}, error: null });
});
afterEach(() => { unmount(); vi.restoreAllMocks(); });

const ask = demoDecision("dec_storage_backups_1")!;

async function open() {
  const { StorageDeleteDecision } = await import("../app/infra/storage-delete-decision");
  await mount(<StorageDeleteDecision ask={ask} projectId="p1" actor="alice" why="backups grew 18 GB this week, one per upgrade, none removed" />);
}

describe("the deletion card", () => {
  it("says exactly what goes, what stays, why, what was already done, and what it will run", async () => {
    await open();
    expect($('[data-testid="sdd-sentence"]')!.textContent).toBe("Delete 348 pre-upgrade backups, freeing 161 GB. Keep the newest 10 and everything from the last 7 days.");
    expect(document.body.textContent).toContain("Not reversible");
    expect(document.body.textContent).toContain("backups grew 18 GB this week");
    expect($('[data-testid="sdd-to-delete"] summary')!.textContent).toBe("348 to delete · 161 GB");
    // Not now is a gated press like the rest (decision:approve).
    expect($('[data-testid="sdd-later"]')!.getAttribute("data-permission")).toBe("decision:approve");
    expect($('[data-testid="sdd-kept"] summary')!.textContent).toBe("10 kept");
    expect($('[data-testid="sdd-tool"]')!.textContent).toContain("tool: storage.remove_backups");
    expect($('[data-testid="sdd-tool"]')!.textContent).toContain("keep_newest: 10");
    expect($('[data-testid="sdd-tool"]')!.textContent).toContain("dry_run: done · 348 matched");
    expect(document.body.textContent).toContain("42 GB");
    expect(document.body.textContent).toContain("14 runs paused");
    for (const word of ["backups", "databases", "data volumes", "anything unlabelled"]) expect(document.body.textContent).toContain(word);
  });

  it("offers the rule pre-ticked when the agent proposes it, and the one tap carries it with the plan hash", async () => {
    await open();
    const rule = $('[data-testid="sdd-make-rule"]') as HTMLInputElement;
    expect(rule.checked).toBe(true);
    expect($('[data-testid="sdd-rule"]')!.textContent).toContain("keep 10 · 7 days · 20 GB cap");
    expect($('[data-testid="sdd-delete"]')!.textContent).toContain("Delete 348 · free 161 GB");
    await click($('[data-testid="sdd-delete"]'));
    expect(answered).toHaveBeenCalledWith("dec_storage_backups_1", "alice", expect.objectContaining({ planHash: "ph_7c1e9a" }),
                                          { action: "approve", makeRule: true });
    expect($('[data-testid="sdd-said"]')!.textContent).toContain("automatic from now on");
    expect($('[data-testid="sdd-delete"]')).toBeNull();
  });

  it("the agent's alternative RE-PLANS the step (a new dry run, a new card); it never approves", async () => {
    await open();
    expect($('[data-testid="sdd-alternative"]')!.textContent).toBe("Keep 20 instead");
    await click($('[data-testid="sdd-alternative"]'));
    expect(replanned).toHaveBeenCalledWith("si_20261114_laptop", "storage.remove_backups", { keep_newest: 20, keep_days: 7, cap_gb: 20 });
    expect(answered).not.toHaveBeenCalled();
    expect($('[data-testid="sdd-said"]')!.dataset.done).toBe("replanned");
    expect($('[data-testid="sdd-said"]')!.textContent).toContain("asks once more");
  });

  it("offers no alternative when the card names no incident to re-plan against, and an unticked rule is not sent", async () => {
    const { StorageDeleteDecision } = await import("../app/infra/storage-delete-decision");
    await mount(<StorageDeleteDecision ask={{ ...ask, incidentId: null }} projectId="p1" actor="alice" />);
    expect($('[data-testid="sdd-alternative"]')).toBeNull();
    await click($('[data-testid="sdd-make-rule"]'));
    await click($('[data-testid="sdd-delete"]'));
    expect(answered).toHaveBeenCalledWith(expect.any(String), "alice", expect.anything(), { action: "approve", makeRule: false });
  });

  it("Not now keeps the runs paused and says the agent asks again", async () => {
    await open();
    await click($('[data-testid="sdd-later"]'));
    expect(answered).toHaveBeenCalledWith(expect.any(String), "alice", expect.anything(), expect.objectContaining({ action: "reject" }));
    expect($('[data-testid="sdd-said"]')!.textContent).toContain("asks again in 1 h");
  });

  it("says what stopped it, in words, and keeps the buttons", async () => {
    answered.mockResolvedValue({ ok: false, status: 409, body: null, error: "The dry run is older than the plan; it is being redone." });
    await open();
    await click($('[data-testid="sdd-delete"]'));
    expect($('[data-testid="sdd-error"]')!.textContent).toContain("dry run is older");
    expect($$('[data-testid="sdd-delete"]')).toHaveLength(1);
  });

  it("is the card for kind storage_delete in Approvals and in Needs you, and sends the contract's approve body", async () => {
    const inbox = codeOnly(join(__dirname, "../app/decision-inbox-ui.tsx"));
    expect(inbox).toMatch(/decision\.kind === "storage_delete"/);
    expect(inbox).toContain("<StorageDeleteFromDecision");
    const card = codeOnly(join(__dirname, "../app/agents/attention-card.tsx"));
    expect(card).toMatch(/decisionKind === "storage_delete"/);
    expect(card).toContain("<StorageDeleteByDecision");
    expect(codeOnly(join(__dirname, "../lib/decisions.ts"))).toContain('"storage_delete"');
    // The real write: approve carries plan_hash, make_rule and the alternative's args.
    answered.mockRestore();
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: JSON.parse(String(init?.body ?? "{}")) });
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }));
    replanned.mockRestore();
    await storage.answerStorageDelete("d1", "alice", ask, { action: "approve", makeRule: true });
    expect(calls[0].url).toContain("/v1/decisions/d1/approve");
    expect(calls[0].body).toEqual({ content_hash: "ph_7c1e9a", answer: { make_rule: true, plan_hash: "ph_7c1e9a" } });
    expect(calls[0].body).not.toHaveProperty("make_rule");
    await storage.replanStorageStep("si_1", "storage.remove_backups", { keep_newest: 20 });
    expect(calls[1].url).toContain("/v1/infra/storage/incidents/si_1/replan");
    expect(calls[1].body).toEqual({ tool: "storage.remove_backups", args: { keep_newest: 20 } });
    vi.unstubAllGlobals();
  });
});
