// @vitest-environment happy-dom
/**
 * The Recommended roadmap (design §14.4; BOSSAPP-911) against the real routes
 * (apps/api/app/routers/ambient_learning.py): GET /v1/ambient/roadmap lists ranked items, each a
 * small diagram (the API's strip) with value / effort / risk as dots; ⓘ reads
 * GET /v1/ambient/roadmap/{id} for the why, the evidence chips and the squad; Accept posts `as`
 * (ticket / epic / PRD panel); Reject needs a reason AND one of the engine's categories; Not now
 * posts `until` = now + the days picked; all are gated on work_item:manage.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { REJECT_CATEGORIES } from "../../lib/ambient";
import { setConsoleLive } from "../../lib/console-live";
import { $$, click, mountAs, settle, tid, type, unmount, type Role } from "../_helpers/multirepo-fixtures";
import type { Call } from "../_helpers/multirepo-fixtures";
import { fakeAmbientApi, writes } from "./_fixtures";

const { Roadmap } = await import("../../app/agent-ops/roadmap");

afterEach(() => { unmount(); window.history.replaceState({}, "", "/"); });

async function open(role: Role = "owner", override?: (c: Call) => { status?: number; body: unknown } | null | undefined) {
  const calls = fakeAmbientApi(override);
  setConsoleLive(true);
  await mountAs(<Roadmap projectId="p1" />, role);
  await settle();
  return calls;
}
const card = (id: string) => $$('[data-testid="aa-rec"]').find((c) => c.dataset.rec === id)!;
const inCard = (id: string, testId: string) => card(id).querySelector<HTMLElement>(`[data-testid="${testId}"]`);

describe("the Recommended roadmap", () => {
  it("reads GET /v1/ambient/roadmap for open items", async () => {
    const calls = await open();
    const read = calls.find((c) => c.method === "GET" && c.path === "/v1/ambient/roadmap");
    expect(read).toBeTruthy();
    expect(new URLSearchParams(read!.search).get("project_id")).toBe("p1");
    expect(new URLSearchParams(read!.search).get("status")).toBe("open");
    expect(calls.some((c) => c.path.includes("/recommendations"))).toBe(false);
  });

  it("ranks cards, draws each as a small diagram with dots, and keeps the why behind ⓘ", async () => {
    const calls = await open();
    expect($$('[data-testid="aa-rec"]').map((c) => c.dataset.rec)).toEqual(["rec_orders_test", "rec_review_lint", "rec_alert_gap", "rec_model_route"]);
    expect(inCard("rec_orders_test", "aa-rec-rank")?.textContent).toBe("#1");
    expect(card("rec_orders_test").querySelectorAll('[data-testid="aa-thumb"] .aa-thumb-node').length).toBeGreaterThanOrEqual(3);
    expect(inCard("rec_orders_test", "aa-rec-value")?.dataset.level).toBe("high");
    expect(inCard("rec_orders_test", "aa-rec-effort")?.dataset.level).toBe("low");
    expect(inCard("rec_model_route", "aa-rec-effort")?.dataset.level).toBe("medium");
    expect(inCard("rec_orders_test", "aa-rec-why")).toBeNull();
    await click(card("rec_orders_test").querySelector<HTMLElement>(".aa-why"));
    await settle();
    expect(calls.some((c) => c.method === "GET" && c.path === "/v1/ambient/roadmap/rec_orders_test")).toBe(true);
    expect(inCard("rec_orders_test", "aa-rec-why")?.textContent).toContain("INC-31 and INC-44");
    expect(inCard("rec_orders_test", "aa-rec-evidence")?.textContent).toContain("INC-31");
    expect(card("rec_orders_test").querySelector('a[href*="kg_node=kgn_orders"]')).toBeTruthy();
  });

  it("accepts as the shape you pick, and the card leaves", async () => {
    const calls = await open();
    await click(inCard("rec_orders_test", "aa-rec-accept"));
    await click(inCard("rec_orders_test", "aa-rec-accept-epic"));
    const w = writes(calls, "/v1/ambient/roadmap/rec_orders_test/accept");
    expect(w[0].body).toEqual({ project_id: "p1", as: "epic" });
    expect($$('[data-testid="aa-rec"]').some((c) => c.dataset.rec === "rec_orders_test")).toBe(false);
  });

  it("rejects only with a reason and one of the engine's categories", async () => {
    const calls = await open();
    await click(inCard("rec_review_lint", "aa-rec-reject"));
    expect($$('[data-testid^="aa-rec-cat-"]').map((b) => b.dataset.testid!.replace("aa-rec-cat-", ""))).toEqual([...REJECT_CATEGORIES]);
    await click(inCard("rec_review_lint", "aa-rec-reject-confirm"));
    expect(writes(calls, /reject/)).toHaveLength(0);
    await type(card("rec_review_lint").querySelector("input"), "we use a different linter");
    await click(inCard("rec_review_lint", "aa-rec-reject-confirm"));
    expect(writes(calls, /reject/)).toHaveLength(0);   // a reason alone is not enough
    await click(inCard("rec_review_lint", "aa-rec-cat-duplicate"));
    expect(inCard("rec_review_lint", "aa-rec-cat-duplicate")?.getAttribute("aria-checked")).toBe("true");
    await click(inCard("rec_review_lint", "aa-rec-reject-confirm"));
    expect(writes(calls, "/v1/ambient/roadmap/rec_review_lint/reject")[0].body)
      .toEqual({ project_id: "p1", reason: "we use a different linter", category: "duplicate" });
  });

  it("Not now posts until = now + the days picked", async () => {
    const calls = await open();
    await click(inCard("rec_alert_gap", "aa-rec-snooze"));
    const before = Date.now();
    await click(inCard("rec_alert_gap", "aa-rec-snooze-30"));
    const w = writes(calls, "/v1/ambient/roadmap/rec_alert_gap/snooze");
    expect(w).toHaveLength(1);
    const body = w[0].body as { project_id: string; until: string; days?: number };
    expect(body.project_id).toBe("p1");
    expect(body.days).toBeUndefined();
    const until = Date.parse(body.until);
    expect(until - before).toBeGreaterThan(29.9 * 86_400_000);
    expect(until - before).toBeLessThan(30.1 * 86_400_000);
  });

  it("gates every button for a viewer", async () => {
    const calls = await open("viewer");
    expect(inCard("rec_orders_test", "aa-rec-accept")?.getAttribute("aria-disabled")).toBe("true");
    await click(inCard("rec_orders_test", "aa-rec-snooze"));
    expect(inCard("rec_orders_test", "aa-rec-snooze-for")).toBeNull();
    expect(writes(calls, /roadmap/)).toHaveLength(0);
  });

  it("says the roadmap is coming while the API has no route for it", async () => {
    await open("owner", (c) => (c.path === "/v1/ambient/roadmap" ? null : undefined));
    expect(tid("aa-roadmap-missing")).toBeTruthy();
  });
});
