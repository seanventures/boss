import { describe, expect, it } from "vitest";

import { IdentityError } from "../lib/identity/api";
import { composeConflicts, parseComposed, refusalCode } from "../lib/templates";

/**
 * BOSSAPP-526: Login + Email (or + Stripe) in ONE build through POST /v1/templates/compose. The
 * console reads the order, the merged egress and one record per connector, and a conflict
 * refusal lists every conflict so the person sees which two connectors clash and on what.
 */
describe("several connectors in one build", () => {
  it("reads the composition's order and one record per connector", () => {
    const out = parseComposed({
      workflow_run_id: "wfr_1", app_key: "shop", order: ["login-oauth", "transactional-email"],
      egress: ["accounts.google.com", "api.resend.com"],
      connectors: [
        { template_key: "login-oauth", order: 1, application_id: "tap_a", receipt_id: "rcpt_a" },
        { template_key: "transactional-email", order: 2, application_id: "tap_b", receipt_id: "rcpt_b" },
      ],
    });
    expect(out.order).toEqual(["login-oauth", "transactional-email"]);
    expect(out.connectors.map((c) => c.receiptId)).toEqual(["rcpt_a", "rcpt_b"]);
    expect(out.workflowRunId).toBe("wfr_1");
  });

  it("lists every conflict of a connector_conflict refusal, and nothing for another refusal", () => {
    const conflict = new IdentityError(409, "conflict" as never, { body: {
      error: "connector_conflict", message: "these connectors cannot be applied together",
      conflicts: [{ kind: "route", what: "GET /api/orders/:id", connectors: ["stripe-checkout", "paypal-checkout"] },
                  { kind: "file", what: "stacks/node/payments/store.js", connectors: ["stripe-checkout", "paypal-checkout"] }],
    } });
    expect(refusalCode(conflict)).toBe("connector_conflict");
    expect(composeConflicts(conflict).map((c) => c.what)).toEqual(["GET /api/orders/:id", "stacks/node/payments/store.js"]);
    const other = new IdentityError(409, "conflict" as never, { body: { error: "secrets_needed" } });
    expect(composeConflicts(other)).toEqual([]);
  });
});
