// @vitest-environment happy-dom
/**
 * The Recommended roadmap (design §14.4; BOSSAPP-911, spec'd shape): ranked cards with value /
 * effort / risk as dots and evidence as chips that open what they cite; Accept asks "as a
 * ticket, epic or PRD panel" and posts that; Reject needs a reason (it teaches the detector);
 * Not now snoozes; the "why" is one tap away; all three are gated on work_item:manage.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

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
  it("ranks cards by score, with dots, evidence chips and the why behind ⓘ", async () => {
    await open();
    expect($$('[data-testid="aa-rec"]').map((c) => c.dataset.rec)).toEqual(["rec_orders_test", "rec_review_lint", "rec_alert_gap", "rec_model_route"]);
    expect(inCard("rec_orders_test", "aa-rec-value")?.dataset.level).toBe("high");
    expect(inCard("rec_orders_test", "aa-rec-effort")?.dataset.level).toBe("low");
    expect(inCard("rec_orders_test", "aa-rec-evidence")?.textContent).toContain("INC-31");
    expect(card("rec_orders_test").querySelector('a[href*="kg_node=kgn_orders"]')).toBeTruthy();
    expect(inCard("rec_orders_test", "aa-rec-why")).toBeNull();
    await click(card("rec_orders_test").querySelector<HTMLElement>(".aa-why"));
    expect(inCard("rec_orders_test", "aa-rec-why")?.textContent).toContain("INC-31 and INC-44");
  });

  it("accepts as the shape you pick, and the card leaves", async () => {
    const calls = await open();
    await click(inCard("rec_orders_test", "aa-rec-accept"));
    await click(inCard("rec_orders_test", "aa-rec-accept-epic"));
    const w = writes(calls, "/v1/ambient/recommendations/rec_orders_test/accept");
    expect(w[0].body).toMatchObject({ project_id: "p1", as: "epic" });
    expect($$('[data-testid="aa-rec"]').some((c) => c.dataset.rec === "rec_orders_test")).toBe(false);
  });

  it("rejects only with a reason", async () => {
    const calls = await open();
    await click(inCard("rec_review_lint", "aa-rec-reject"));
    const confirm = inCard("rec_review_lint", "aa-rec-reject-confirm")!;
    await click(confirm);
    expect(writes(calls, /reject/)).toHaveLength(0);
    await type(card("rec_review_lint").querySelector("input"), "we use a different linter");
    await click(inCard("rec_review_lint", "aa-rec-reject-confirm"));
    expect(writes(calls, "/v1/ambient/recommendations/rec_review_lint/reject")[0].body).toMatchObject({ reason: "we use a different linter" });
  });

  it("snoozes with Not now", async () => {
    const calls = await open();
    await click(inCard("rec_alert_gap", "aa-rec-snooze"));
    expect(writes(calls, "/v1/ambient/recommendations/rec_alert_gap/snooze")).toHaveLength(1);
  });

  it("gates every button for a viewer", async () => {
    const calls = await open("viewer");
    expect(inCard("rec_orders_test", "aa-rec-accept")?.getAttribute("aria-disabled")).toBe("true");
    await click(inCard("rec_orders_test", "aa-rec-snooze"));
    expect(writes(calls, /recommendations/)).toHaveLength(0);
  });

  it("says the roadmap is coming while the API has no route for it", async () => {
    await open("owner", (c) => (c.path === "/v1/ambient/recommendations" ? null : undefined));
    expect(tid("aa-roadmap-missing")).toBeTruthy();
  });
});
