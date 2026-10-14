// @vitest-environment happy-dom
/**
 * A connection proof that ended in a hold says why in words, blocks Connect for its provider,
 * and a person releases it from the dialog: Review shows the receipt's facts, "Release the hold"
 * needs a reason, and Connect works again. A refusal shows the provider's own reason. The proof
 * asks exactly the model typed.
 */
import { afterEach, describe, expect, it } from "vitest";

import type { HeldProof, SetupView } from "../lib/setup-types";
import { click, fakeApi, flush, mountBoard, q, type, type Mounted } from "./_helpers/setup-board";
import { repoDone } from "./_helpers/setup-fixtures";

const KEY = "sk-ant-api03-secretsecretsecretsecret";
const SUMMARY = "Anthropic replied OK, but BOSS has no price for claude-sonnet-4-5; the cost "
  + "was estimated conservatively and labelled unpriced.";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

const HOLD: HeldProof = {
  receipt_id: "rcpt_916d535aefb34387bfd7", created_at: "2026-10-14T04:04:22Z",
  provider: "anthropic", provider_label: "Anthropic", model: "claude-sonnet-4-5", actor: "ceo",
  held_usd_micros: 50_000, summary: SUMMARY,
  outcome: { kind: "replied_ok", http_status: null, usage_present: null },
  release: { cost_usd_micros: 2160, basis: "unpriced", succeeded: true },
};

function held(): SetupView { return { ...repoDone(), held_proofs: [HOLD] }; }

describe("a held proof", () => {
  it("says why, blocks Connect, and is released with a reason from Review", async () => {
    const api = fakeApi(held());
    api.answer = (call) => {
      if (call.op === "release_hold") { api.view = repoDone(); return { status: 200, body: repoDone() }; }
      return null;
    };
    mounted = await mountBoard(held(), "?area=setup&add=anthropic");
    expect(q(document.body, "su-held-summary")?.textContent).toContain(SUMMARY);
    await type(q(document.body, "su-model-key"), KEY);
    expect((q(document.body, "su-model-connect") as HTMLButtonElement).disabled).toBe(true);

    await click(q(document.body, "su-held-review"));
    const facts = q(document.body, "su-held-facts");
    expect(facts?.textContent).toContain("rcpt_916d535aefb34387bfd7");
    expect(facts?.textContent).toContain("claude-sonnet-4-5");
    expect(q(document.body, "su-held-charge")?.textContent).toBe("$0.0022 (unpriced estimate)");
    expect((q(document.body, "su-held-release") as HTMLButtonElement).disabled).toBe(true);
    await type(q(document.body, "su-held-reason"), "Anthropic answered; no price was known.");
    await click(q(document.body, "su-held-release"));
    await flush();
    const sent = api.calls.find((c) => c.op === "release_hold");
    expect(sent?.body).toMatchObject({ receipt_id: HOLD.receipt_id,
                                       reason: "Anthropic answered; no price was known." });
    expect(q(document.body, "su-held-proof")).toBeNull();
    await type(q(document.body, "su-model-key"), KEY);
    expect((q(document.body, "su-model-connect") as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("a refused proof", () => {
  it("shows the provider's reason in words", async () => {
    const api = fakeApi(repoDone());
    api.answer = (call) => call.op === "models"
      ? { status: 422, body: { error: "x", refusal: { state: "no_credit", headline: "No credit",
          fix: "Anthropic refused: credit balance too low. Add credit.",
          summary: "Anthropic refused: credit balance too low." } } }
      : null;
    mounted = await mountBoard(repoDone(), "?area=setup&add=anthropic");
    await type(q(document.body, "su-model-key"), KEY);
    await click(q(document.body, "su-model-connect"));
    expect(q(document.body, "su-refusal-summary")?.textContent)
      .toBe("Anthropic refused: credit balance too low.");
  });
});

describe("the proof's model", () => {
  it("is exactly the model typed, and an empty model cannot Connect", async () => {
    const api = fakeApi(repoDone());
    mounted = await mountBoard(repoDone(), "?area=setup&add=anthropic");
    const model = q(document.body, "su-model-name") as HTMLInputElement;
    expect(model.value).toBe("claude-sonnet-5");
    await type(q(document.body, "su-model-key"), KEY);
    await type(model, "");
    expect((q(document.body, "su-model-connect") as HTMLButtonElement).disabled).toBe(true);
    await type(model, "claude-opus-5-5");
    await click(q(document.body, "su-model-connect"));
    expect(api.calls.find((c) => c.op === "models")?.body.model).toBe("claude-opus-5-5");
  });
});
