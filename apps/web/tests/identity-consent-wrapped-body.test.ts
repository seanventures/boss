import { describe, expect, it } from "vitest";
import { consentFromBody } from "../lib/identity/consent";

// Integration walk: the console's /api/delivery-workflows relays a 409 as
// {error: "consent_required", consent: {...the API's body...}}; the sheet read the wrapper's own
// (empty) fields, offered "Standing" for no agents, and the grant was refused as empty.
describe("a wrapped consent_required body", () => {
  const inner = {
    error: "consent_required", scope: "workflow_run", task: "fix", task_run_id: "pending:abc",
    agents: [{ id: "agt_1", name: "Captain", role_key: "engineering_captain" }],
    permissions: ["code_task:read"], projects: ["p1"], github: null,
    durations: [{ kind: "task", label: "For this run only", days: null }, { kind: "standing", label: "For 30 days", days: 30 }],
  };
  it("is read from the inner body even when the wrapper repeats the error", () => {
    const r = consentFromBody({ error: "consent_required", consent: inner });
    expect(r?.agents.map((a) => a.id)).toEqual(["agt_1"]);
    expect(r?.permissions).toEqual(["code_task:read"]);
    expect(r?.task_run_id).toBe("pending:abc");
    expect(r?.durations.map((d) => d.kind)).toEqual(["task", "standing"]);
  });
  it("and a flat body still works", () => {
    expect(consentFromBody(inner)?.agents.length).toBe(1);
  });
});
