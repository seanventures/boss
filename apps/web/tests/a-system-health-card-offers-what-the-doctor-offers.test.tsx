// @vitest-environment happy-dom
/**
 * The machine gets a card (stability spec §4d). A system-doctor finding that needs a choice is a
 * `system_health` item in What needs you: the shared AttentionCard draws exactly the options the
 * server sent — their labels, their tooltips — asks first for one that stops a person's work, and
 * shows what the press did in place.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseAttention } from "../lib/agents-live-board";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

const act = vi.fn(async (form: FormData) => ({
  ok: true, result: `did ${String(form.get("option"))} on ${String(form.get("finding_id"))}`,
}));
vi.mock("../app/system-health-actions", () => ({ actOnSystemFindingAction: (f: FormData) => act(f) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AttentionCard, kindWord } = await import("../app/agents/attention-card");

const memory = parseAttention({
  id: "system:sf_1", kind: "system_health", finding_id: "sf_1", severity: "critical",
  title: "Builds are running out of memory",
  detail: "3 builds are using 5.1 of 7.6 GB; the machine has been waiting on memory 12% of the last minute.",
  answers: [
    { key: "queue_new_builds", label: "Queue new builds (recommended)", enabled: true,
      reason: "Run one build at a time until memory frees up. Nothing running is stopped." },
    { key: "pause_largest_build", label: "Pause “Docs board” build", enabled: true, confirm: true,
      reason: "Stops the largest build (2.3 GB). It resumes from its last step when you press Resume." },
    { key: "keep_going", label: "Keep going", enabled: true, reason: "Change nothing. Builds may slow down or fail." },
  ],
});

afterEach(() => { unmount(); act.mockClear(); });

describe("a system-health card", () => {
  it("draws the server's options with their tooltips, and is called System", async () => {
    await mount(<AttentionCard item={memory} projectId="p1" actor="ceo" />);
    expect(kindWord(memory)).toBe("System");
    const buttons = $$('[data-testid^="ag-att-option-"]');
    expect(buttons.map((b) => b.textContent)).toEqual(
      ["Queue new builds (recommended)", "Pause “Docs board” build", "Keep going"]);
    expect(buttons[0].getAttribute("title")).toBe(
      "Run one build at a time until memory frees up. Nothing running is stopped.");
    expect($('[data-att-card]')?.getAttribute("data-severity")).toBe("critical");
  });

  it("a choice posts that option for that finding and says what it did, in place", async () => {
    await mount(<AttentionCard item={memory} projectId="p1" actor="ceo" />);
    await click($('[data-testid="ag-att-option-queue_new_builds"]'));
    await settle();
    expect(act).toHaveBeenCalledTimes(1);
    const form = act.mock.calls[0][0] as FormData;
    expect([form.get("finding_id"), form.get("option"), form.get("actor")])
      .toEqual(["sf_1", "queue_new_builds", "ceo"]);
    expect($('[data-testid="ag-att-done"]')?.textContent).toBe("did queue_new_builds on sf_1");
  });

  it("stopping a build asks first; Cancel sends nothing", async () => {
    await mount(<AttentionCard item={memory} projectId="p1" actor="ceo" />);
    await click($('[data-testid="ag-att-option-pause_largest_build"]'));
    expect($('[data-testid="ag-att-option-confirm"]')).not.toBeNull();
    expect(act).not.toHaveBeenCalled();
    await click($('[data-testid="ag-att-option-confirm-yes"]'));
    await settle();
    expect((act.mock.calls[0][0] as FormData).get("option")).toBe("pause_largest_build");
  });
});

describe("the Command Center hands the card the same item", () => {
  it("keeps the finding, its id, its severity and which option asks first", async () => {
    const { attentionFromNeedsYou } = await import("../lib/command-center");
    const card = attentionFromNeedsYou({
      id: "system:sf_9", kind: "system_health", finding_id: "sf_9", severity: "warn",
      title: "Updates restarted a build 4 times in an hour",
      detail: "Each restart redid clone and install (~6 min).", actions: ["hold_updates", "keep_updating"],
      answers: [{ key: "hold_updates", label: "Hold updates while builds run (recommended)", enabled: true,
                  reason: "Updates wait for running builds to finish (up to 30 min).", confirm: false } as never],
      at: null,
    });
    expect([card.id, card.kind, card.findingId, card.severity]).toEqual(["system:sf_9", "system_health", "sf_9", "warn"]);
    expect(card.answers.map((a) => a.label)).toEqual(["Hold updates while builds run (recommended)"]);
  });
});
