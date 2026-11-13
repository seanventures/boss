// @vitest-environment happy-dom

import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { SecretsSheet } from "../app/infra/secrets-sheet";
import { mockInfraFetch, type Route } from "./_helpers/infra-fixtures";
import { allByTestId, byTestId, mount, press, settle, type Mounted } from "./_helpers/infra-tab-render";

/**
 * BOSSAPP-865: Secrets › Access log — who read a secret (run, person, agent, version), and
 * refusals, newest first. Metadata only: the sheet never receives a value.
 */

const SECRETS = {
  secrets: [{ secret_ref_id: "sref_1", name: "BOSS_P_STRIPE_KEY", backend: "database", secret_kind: "api_token",
    status: "active", fingerprint: null, last4: "9xyz", bound_to: [] }],
};
const LOG = {
  version: 2,
  versions: [{ version: 2, fingerprint: null, last4: "9xyz", created_by: "usr_a", created_at: "2026-11-13T10:00:00Z" },
    { version: 1, fingerprint: null, last4: "1abc", created_by: "usr_a", created_at: "2026-11-12T10:00:00Z" }],
  events: [
    { id: "sae_2", version: 2, outcome: "refused", reason: "scoped to another agent", run_id: "ctr_9",
      person_id: null, agent_role: "reviewer", principal: null, accessed_at: "2026-11-13T11:00:00Z" },
    { id: "sae_1", version: 2, outcome: "resolved", reason: null, run_id: "ctr_8",
      person_id: "usr_a", agent_role: "deployer", principal: null, accessed_at: "2026-11-13T10:30:00Z" },
  ],
};

let m: Mounted | null = null;
let restore: (() => void) | null = null;
afterEach(async () => { await m?.unmount(); m = null; restore?.(); restore = null; });

describe("Secrets › Access log", () => {
  it("opens beneath the row with the runs, agents and versions that read it", async () => {
    const routes: Route[] = [
      { match: /^\/v1\/secrets$/, reply: () => SECRETS },
      { match: /^\/v1\/secrets\/sref_1\/access-log$/, reply: () => LOG },
    ];
    const f = mockInfraFetch({ routes });
    restore = f.install();
    m = await mount(<SecretsSheet projectId="project_1" actor="ceo" onClose={() => {}} />);
    await settle(() => !!byTestId("inf-secret-log-sref_1"), "secret row");
    await press(byTestId("inf-secret-log-sref_1"), "Access log");
    await settle(() => allByTestId("inf-secret-access-row").length === 2, "access rows");
    const rows = allByTestId("inf-secret-access-row");
    expect(rows[0].textContent).toContain("Refused");
    expect(rows[0].textContent).toContain("reviewer");
    expect(rows[1].textContent).toContain("Read");
    expect(rows[1].textContent).toContain("ctr_8");
    expect(byTestId("inf-secret-versions")!.textContent).toContain("Version 2");
    expect(f.calls.some((c) => c.url.includes("/v1/secrets/sref_1/access-log?project_id=project_1"))).toBe(true);
    await press(byTestId("inf-secret-log-sref_1"), "Access log (close)");
    expect(byTestId("inf-secret-access-log")).toBeNull();
  });
});
