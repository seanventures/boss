/**
 * Every built-in connector has its own mark on its card (Login, Stripe, PayPal, Email): a new
 * connector under templates/apps/ with `labels: [connector]` would otherwise ship showing the
 * puzzle piece kept for imported connectors. This fails first.
 */
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { MARKED_CONNECTORS } from "../components/v2/templates/catalogue";

const TEMPLATES = join(__dirname, "../../../templates/apps");

/** The built-ins the server offers as connectors: `labels: [connector]` in their boss-template.yaml. */
function builtInConnectors(): string[] {
  return readdirSync(TEMPLATES)
    .map((d) => join(TEMPLATES, d, "boss-template.yaml"))
    .filter((f) => existsSync(f))
    .map((f) => readFileSync(f, "utf8"))
    .filter((y) => /^labels:\s*\[[^\]]*\bconnector\b[^\]]*\]/m.test(y))
    .map((y) => y.match(/^id:\s*(\S+)\s*$/m)![1]);
}

describe("every connector has its mark", () => {
  it("reads the built-ins, the email connector among them", () => {
    expect(builtInConnectors()).toEqual(expect.arrayContaining(["login-oauth", "stripe-checkout", "paypal-checkout", "transactional-email"]));
  });

  it("no built-in connector is left with the puzzle piece, and no mark is orphaned", () => {
    expect([...builtInConnectors()].sort()).toEqual([...MARKED_CONNECTORS].sort());
  });
});
