// @vitest-environment happy-dom

import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { readOnly, cloudFixture } from "./_helpers/infra-cloud-fixtures";
import { $, $$, mountCloud, press, settle, type, type Mounted } from "./_helpers/infra-cloud-render";

/**
 * Infra › Cloud renders every sub-view from GET /v1/infra/cloud (spec §4.8), keeps every action
 * the old cloud screens had (parity P-S-03..P-S-45), and never shows micros or the old words.
 * Rendered against the shared fixtures; presses go through the real write helpers.
 */

let m: Mounted | null = null;
afterEach(async () => { await m?.done(); m = null; });

const text = () => document.body.textContent ?? "";
const BANNED = /micros|ChangeSet|ProviderOps|Resource Plane|SecretRef|elastic fabric|entitlement|governed|fail-closed|idempotent/i;

async function open(search: string, ready: string) {
  m = await mountCloud(search);
  await settle(() => !!$(ready), ready);
}

describe("Infra › Cloud", () => {
  it("Accounts: table, counts, the BYOK chip, and the + Account catalog with both connect forms", async () => {
    await open("?area=infra&view=cloud", "inf-cloud-table");
    expect($("inf-cloud-seg")?.textContent).toContain("Accounts2");
    expect($("inf-cloud-notice")?.textContent).toBe("Bring your own key");
    expect($("inf-cloud-audit")).not.toBeNull();
    expect($("inf-cloud-table")?.textContent).toContain("$41.20");
    expect($("inf-cloud-table")?.textContent).toContain("<$0.01");
    expect(text()).not.toContain("41200000");
    expect(text()).not.toMatch(BANNED);
    expect(text()).not.toContain("Elastic Resource Plane");
    await press("inf-account-new");
    for (const id of ["inf-account-catalog", "inf-account-paste", "inf-account-secret-ref", "inf-catalog-adapter"]) expect($(id), id).not.toBeNull();
    expect($("inf-account-catalog")?.textContent).toContain("12 skills");
  });

  it("Paste key sends the value once, to the vault route, and clears it", async () => {
    await open("?area=infra&view=cloud", "inf-account-new");
    await press("inf-account-new");
    await type($("inf-paste-label"), "DO prod");
    await type($("inf-paste-secret"), "dop_v1_abcdefabcdef");
    await act(async () => { ($("inf-paste-submit") as HTMLButtonElement).click(); });
    await settle(() => m!.api.writes().length > 0, "paste post");
    const w = m!.api.writes()[0];
    expect(w.path).toBe("/v1/providers/easy-connect");
    expect(w.body).toMatchObject({ provider: "digitalocean", account_label: "DO prod", backend: "local_vault", read_only: true });
  });

  it("Use secret ref refuses a pasted secret and posts references", async () => {
    await open("?area=infra&view=cloud", "inf-account-new");
    await press("inf-account-new");
    await type($("inf-ref-label"), "DO prod");
    await type($("inf-ref-slot-api_token"), "-----BEGIN OPENSSH PRIVATE KEY----- abc");
    await act(async () => { ($("inf-ref-submit") as HTMLButtonElement).click(); });
    expect($("inf-account-sheet-error")?.textContent).toContain("private key");
    expect(m!.api.writes()).toHaveLength(0);
    await type($("inf-ref-slot-api_token"), "env:BOSS_DO_TOKEN");
    await act(async () => { ($("inf-ref-submit") as HTMLButtonElement).click(); });
    await settle(() => m!.api.writes().length > 0, "connect post");
    expect(m!.api.writes()[0]).toMatchObject({ path: "/v1/providers", body: { credential_refs: { api_token: "env:BOSS_DO_TOKEN" }, read_only: true } });
  });

  it("the account drawer: impact, credentials, Test, Rotate with its overlap, Audit, and a typed Revoke", async () => {
    await open("?area=infra&view=cloud&account=acct_1", "inf-account-drawer");
    for (const id of ["inf-account-impact", "inf-account-credentials", "inf-account-test", "inf-account-rotate", "inf-account-revoke", "inf-account-audit"]) {
      expect($(id), id).not.toBeNull();
    }
    expect($("inf-account-credentials")?.textContent).toContain("•••••• set");
    expect($("inf-account-audit")?.getAttribute("href")).toBe("?area=ops&provider_account_id=acct_1");

    await type($("inf-account-overlap"), "15");
    await type($("inf-account-rotate-api_token"), "env:NEW_DO_TOKEN");
    await act(async () => { ($("inf-account-rotate")!.querySelector("button[type=submit]") as HTMLButtonElement).click(); });
    await settle(() => m!.api.writes().length === 1, "rotate");
    expect(m!.api.writes()[0]).toMatchObject({ path: "/v1/providers/acct_1/rotate", body: { overlap_minutes: 15, credential_refs: { api_token: "env:NEW_DO_TOKEN" } } });

    await press("inf-account-revoke");
    const confirm = () => $("inf-account-revoke-confirm") as HTMLButtonElement;
    expect(confirm().disabled).toBe(true);
    await type($("inf-account-revoke-name"), "DigitalOcean");
    expect(confirm().disabled).toBe(true);
    await type($("inf-account-revoke-name"), "DigitalOcean · prod");
    expect(confirm().disabled).toBe(false);
    await act(async () => { confirm().click(); });
    await settle(() => m!.api.writes().length === 2, "revoke");
    expect(m!.api.writes()[1].path).toBe("/v1/providers/acct_1/revoke");
  });

  it("Resources: scope chips, droplets as rows, Sync all, Clean up…, and the resource drawer", async () => {
    await open("?area=infra&view=cloud&cloud=resources&resource=res_1", "inf-resource-drawer");
    for (const id of ["inf-cloud-filters", "inf-cloud-table", "inf-resource-droplet", "inf-resources-sync-all", "inf-resources-cleanup"]) {
      expect($(id), id).not.toBeNull();
    }
    expect($("inf-resource-droplet")?.textContent).toContain("4h");
    expect($("inf-resource-drawer")?.textContent).toContain("db-7f3a");
    await type($("inf-cloud-filter-provider"), "aws");
    await settle(() => m!.api.gets(/\/v1\/infra\/cloud$/).some((c) => c.url.includes("provider=aws")), "scoped read");
    await settle(() => !$("inf-resource-droplet"), "scope applied");
    await press("inf-resources-sync-all");
    await settle(() => m!.api.writes().length === 1, "sync all");
    expect(m!.api.writes()[0]).toMatchObject({ path: "/v1/resources/sync" });
    await press("inf-resources-cleanup");
    await type($("inf-cleanup-max"), "7");
    await press("inf-cleanup-submit");
    await settle(() => m!.api.writes().length === 2, "clean up");
    expect(m!.api.writes()[1]).toMatchObject({ path: "/v1/resource-janitor/run", body: { max_candidates: 7 } });
  });

  it("Capacity: queue, counts, API health, plans, pool drawer history, and + Pool in dollars", async () => {
    await open("?area=infra&view=cloud&cloud=capacity&pool=pool_1", "inf-pool-drawer");
    for (const id of ["inf-capacity-queue", "inf-capacity-counts", "inf-cloud-api-health", "inf-cloud-table", "inf-capacity-plan",
      "inf-pool-history", "inf-capacity-add-pool", "inf-capacity-review"]) expect($(id), id).not.toBeNull();
    expect($("inf-capacity-queue")?.textContent).toContain("Queue 12 · Leased 2 · Running 2");
    expect($("inf-pool-history")?.textContent).toContain("1 → 2");
    await act(async () => { ($("inf-pool-drawer-close") as HTMLButtonElement).click(); });
    await press("inf-capacity-add-pool");
    await type($("inf-pool-key"), "burst");
    await type($("inf-pool-cost"), "2.5");
    await press("inf-pool-submit");
    await settle(() => m!.api.writes().length === 1, "pool post");
    expect(m!.api.writes()[0]).toMatchObject({ path: "/v1/worker-pools", body: { pool_key: "burst", cost_per_worker_hour_usd_micros: 2_500_000 } });
    await press("inf-capacity-review");
    await act(async () => { ($("inf-review-spill") as HTMLInputElement).click(); });
    await press("inf-review-submit");
    await settle(() => m!.api.writes().length === 2, "review post");
    expect(m!.api.writes()[1]).toMatchObject({ path: "/v1/autoscaler/plans", body: { cloud_spillover_enabled: true } });
  });

  it("Spend: bars from real sums, anomalies with Set a cap, est., and the provider drawer", async () => {
    await open("?area=infra&view=cloud&cloud=spend&account=aws", "inf-provider-drawer");
    for (const id of ["inf-spend-bars", "inf-spend-anomalies", "inf-cloud-table"]) expect($(id), id).not.toBeNull();
    expect($("inf-spend-bars")!.querySelectorAll("rect.cloud-bar-fill")).toHaveLength(2);
    expect($("inf-spend-bars")?.textContent).toContain("est.");
    expect($("inf-spend-anomalies")?.querySelector("a")?.getAttribute("title")).toBe("Limits live in Govern");
    expect($("inf-provider-drawer")?.textContent).toContain("storage");
    expect(text()).not.toMatch(/\$0(?!\.\d)/);
  });

  it("Changes: Approve and drift in the drawer; + Change refuses broken JSON with its line, posts dollars as micros", async () => {
    await open("?area=infra&view=cloud&cloud=changes&changeset=cs_1", "inf-change-drawer");
    expect($("inf-change-drift")?.textContent).toContain("unmanaged");
    await press("inf-change-approve");
    await settle(() => m!.api.writes().length === 1, "approve");
    expect(m!.api.writes()[0].path).toBe("/v1/changesets/cs_1/approve");
    await act(async () => { ($("inf-change-drawer-close") as HTMLButtonElement).click(); });

    await press("inf-change-new");
    await type($("inf-change-title"), "Add a volume");
    await type($("inf-change-summary"), "More disk");
    await type($("inf-change-ops"), "[\n  {\"skill_id\": \"do.volume\",\n  \"action\": \"create\"\n  \"resource_type\": \"volume\"}\n]");
    expect($("inf-change-ops-error")?.textContent).toMatch(/line \d/);
    expect(($("inf-change-submit") as HTMLButtonElement).disabled).toBe(true);
    await type($("inf-change-ops"), "[\n  {\"skill_id\": \"do.volume\", \"action\": \"create\"}\n]");
    expect($("inf-change-ops-error")?.textContent).toBe("Operation 1 needs resource_type · line 2");
    await type($("inf-change-ops"), "[{\"skill_id\": \"do.volume\", \"action\": \"create\", \"resource_type\": \"volume\"}]");
    await type($("inf-change-cost"), "12.40");
    await press("inf-change-submit");
    await settle(() => m!.api.writes().length === 2, "change post");
    expect(m!.api.writes()[1]).toMatchObject({ path: "/v1/changesets", body: { title: "Add a volume", expected_cost_usd_micros: 12_400_000,
      operations: [{ skill_id: "do.volume", action: "create", resource_type: "volume" }] } });
  });

  it("a read-only viewer sees the Read only chip and no write control on any sub-view", async () => {
    const route = { match: /^\/v1\/infra\/cloud$/, etag: true, reply: (u: URL) => readOnly(cloudFixture(u.searchParams.get("view") ?? "accounts")) };
    m = await mountCloud("?area=infra&view=cloud&account=acct_1", [route]);
    await settle(() => !!$("inf-account-drawer"), "drawer");
    expect($("inf-cloud-notice")?.textContent).toBe("Read only");
    for (const id of ["inf-account-new", "inf-account-test", "inf-account-rotate", "inf-account-revoke"]) expect($(id), id).toBeNull();
    expect($("inf-account-impact")).not.toBeNull();
    for (const [view, gone] of [["resources", ["inf-resources-sync-all", "inf-resources-cleanup"]], ["capacity", ["inf-capacity-add-pool", "inf-capacity-review"]],
      ["changes", ["inf-change-new"]]] as const) {
      await press(`inf-cloud-seg-${view}`);
      await settle(() => !!$("inf-cloud-table") && $$("inf-cloud-seg").length === 1 && new URLSearchParams(location.search).get("cloud") === view
        && !!document.querySelector(`[aria-labelledby="inf-cloud-${view}"]`), view);
      for (const id of gone) expect($(id), `${view} ${id}`).toBeNull();
    }
  });

  it("keeps the copy budget on every sub-view: no paragraph, no run over six words outside data", async () => {
    const views: [string, string][] = [["", "acct_1"], ["resources", "res_1"], ["capacity", "pool_1"], ["spend", "aws"], ["changes", "cs_1"]];
    const drawerParam: Record<string, string> = { "": "account", resources: "resource", capacity: "pool", spend: "account", changes: "changeset" };
    for (const [view, id] of views) {
      m = await mountCloud(`?area=infra&view=cloud${view ? `&cloud=${view}` : ""}&${drawerParam[view]}=${id}`);
      await settle(() => !!$("inf-cloud-table") && !!document.querySelector("[role=dialog]"), view || "accounts");
      const copy = document.body.cloneNode(true) as HTMLElement;
      copy.querySelectorAll("[data-copy='data']").forEach((el) => el.remove());
      expect(copy.querySelectorAll("p"), view).toHaveLength(0);
      expect(overBudget(copy), view || "accounts").toEqual([]);
      expect(document.body.textContent, view).not.toMatch(BANNED);
      await m.done(); m = null;
    }
  });
});
