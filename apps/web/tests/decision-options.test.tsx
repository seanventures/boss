// @vitest-environment happy-dom
/**
 * A DECISION'S OPTIONS WITH SIGNED EFFECTS (BOSSAPP-879): one Choose per option, its effects in
 * words, and Choose is the card's approve with `choose` — the option id the engine offered.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DecisionOptions, effectWords, optionsOf } from "../app/decision-options-ui";
import type { DecisionActionOffer, DecisionFeedItem } from "../lib/decisions";
import { $, mount, settle, unmount } from "./_helpers/live-render";

const REC = {
  action: "choose_option",
  effect_options: [
    { id: "cancel", label: "Cancel the epic", effects: [{ type: "cancel_tree", target: "wi_1" }], sig: "hs1:x" },
    { id: "qa", label: "Give it to QA", detail: "QA has room this week",
      effects: [{ type: "assign", target: "wi_2", assignee: "agent:qa" }, { type: "create", title: "QA plan" }], sig: "hs1:y" },
  ],
};
const OFFERS: DecisionActionOffer[] = [
  { action: "approve", label: "Cancel the epic", tone: "approve", effect: "Carries out: …", choose: "cancel" },
  { action: "approve", label: "Give it to QA", tone: "approve", effect: "Carries out: …", choose: "qa" },
  { action: "reject", label: "None of these", tone: "reject", effect: "" },
];
const card = (extra: Record<string, unknown> = {}) =>
  ({ id: "dec_9", status: "pending", recommendation: { ...REC, ...extra } }) as unknown as DecisionFeedItem;

afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("decision options", () => {
  it("reads the options and says each effect in words", () => {
    const opts = optionsOf(REC);
    expect(opts.map((o) => o.id)).toEqual(["cancel", "qa"]);
    expect(opts[1].effects.map(effectWords)).toEqual(["Assign wi_2 to agent:qa", "Create “QA plan”"]);
    expect(optionsOf({ action: "execution_stage" })).toEqual([]);
  });

  it("Choose sends the option id as the approve; None of these rejects", async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body ?? "{}")) });
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    await mount(<DecisionOptions decision={card()} projectId="p1" actorId="ceo" offers={OFFERS} />);
    expect($('[data-testid="decision-option-qa"]')?.textContent).toContain("Assign wi_2 to agent:qa");
    ($('[data-testid="decision-choose-qa"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url.includes("/v1/decisions/dec_9/approve"))?.body).toEqual({ actor: "ceo", choose: "qa" });
    ($('[data-testid="decision-options-none"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url.includes("/v1/decisions/dec_9/reject"))?.body).toEqual({ actor: "ceo" });
  });

  it("an option the engine did not offer cannot be chosen, and a chosen card says which", async () => {
    await mount(<DecisionOptions decision={card()} projectId="p1" actorId="ceo" offers={OFFERS.slice(1)} />);
    expect(($('[data-testid="decision-choose-cancel"]') as HTMLButtonElement).disabled).toBe(true);
    unmount();
    await mount(<DecisionOptions decision={card({ chosen_option: "qa" })} projectId="p1" actorId="ceo" offers={[]} />);
    expect($('[data-testid="options-chosen"]')?.textContent).toBe("Chosen: Give it to QA");
  });
});
