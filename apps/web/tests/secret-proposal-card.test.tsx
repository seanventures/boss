// @vitest-environment happy-dom
/**
 * AN AGENT PROPOSED A SECRET (BOSSAPP-871): the person types the value on the card, Store sends
 * it once to /v1/secret-proposals/{id}/store and clears the field; Decline is the card's reject.
 * The agent never sends a value — the card only ever reads it from the person's input.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SecretProposalCard, secretProposalOf } from "../app/secret-proposal-card";
import { $, mount, settle, unmount } from "./_helpers/live-render";

const REC = { action: "store_proposed_secret", name: "stripe_webhook_secret", role_key: "deployer",
  reason: "Checkout must verify webhooks.", target_kind: "deployment_target", target_id: "dt_1",
  binding_name: "STRIPE_WEBHOOK_SECRET" };

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function type(el: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function stubFetch() {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), body: JSON.parse(String(init?.body ?? "{}")) });
    return new Response(JSON.stringify({ status: "approved" }), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

describe("the proposed-secret card", () => {
  it("reads the proposal from the card's recommendation only", () => {
    expect(secretProposalOf(REC)).toMatchObject({ name: "stripe_webhook_secret", target_kind: "deployment_target" });
    expect(secretProposalOf({ action: "grant_tool_access", name: "x" })).toBeNull();
  });

  it("stores the value the person typed, once, and clears it", async () => {
    const calls = stubFetch();
    const done = vi.fn();
    await mount(<SecretProposalCard decisionId="dec_1" proposal={secretProposalOf(REC)!} actorId="ceo" onDone={done} />);
    expect($('[data-testid="secret-proposal-card"]')?.textContent).toContain("deployer asks you to store");
    expect(($('[data-testid="secret-proposal-store"]') as HTMLButtonElement).disabled).toBe(true);
    type($('[data-testid="secret-proposal-value"]') as HTMLInputElement, "whsec_typed");
    await settle();
    ($('[data-testid="secret-proposal-store"]') as HTMLButtonElement).click();
    await settle();
    const sent = calls.filter((c) => c.url.includes("/v1/secret-proposals/dec_1/store"));
    expect(sent).toHaveLength(1);
    expect(sent[0].body).toEqual({ actor: "ceo", value: "whsec_typed" });
    expect(($('[data-testid="secret-proposal-value"]') as HTMLInputElement | null)?.value ?? "").toBe("");
    expect(done).toHaveBeenCalledWith("stored");
  });

  it("declines through the card's reject", async () => {
    const calls = stubFetch();
    await mount(<SecretProposalCard decisionId="dec_2" proposal={secretProposalOf(REC)!} actorId="ceo" />);
    ($('[data-testid="secret-proposal-decline"]') as HTMLButtonElement).click();
    await settle();
    type($('[data-testid="secret-proposal-why"]') as HTMLInputElement, "not this one");
    await settle();
    ($('[data-testid="secret-proposal-decline"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url.includes("/v1/decisions/dec_2/reject"))?.body).toEqual({ actor: "ceo", note: "not this one" });
  });
});
