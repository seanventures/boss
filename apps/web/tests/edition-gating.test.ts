import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { BossApiError } from "@boss/api-client";
import {
  editionOptional,
  editionOptionalList,
  isEditionGated,
  EDITION_GATED_STATUSES,
} from "../lib/edition-gating";

/**
 * A 404 from a feature this edition does not have is not an error.
 *
 * The console printed "API returned 404" ten times on every load — five panels, each counted
 * twice — and none of it was in the area being viewed. `/v1/marketplace/*` and the enterprise
 * identity routes are mounted only on paid editions; this instance runs Community, so 404 is
 * the correct answer to the question asked.
 *
 * The worse half was `Promise.all`. It rejects on the FIRST rejection, so one gated route among
 * seven discarded all seven — three SSO cards showed an API error with their data already
 * loaded, one of them badged "included in Community".
 */

const err = (status: number) =>
  Object.assign(new BossApiError(`HTTP ${status}`, status as never), { status });

describe("edition gating", () => {
  it("treats not-mounted statuses as an edition fact", () => {
    for (const s of EDITION_GATED_STATUSES) expect(isEditionGated(err(s))).toBe(true);
  });

  it("does NOT treat a server error as a licensing fact", async () => {
    // commercial.ts's private helper returned undefined from BOTH branches, so a 500 was
    // silently relabelled "not licensed" — an outage rendered as a missing feature.
    expect(isEditionGated(err(500))).toBe(false);
    await expect(editionOptional(Promise.reject(err(500)), "fallback")).rejects.toBeTruthy();
  });

  it("does not swallow a non-API error either", async () => {
    await expect(editionOptional(Promise.reject(new Error("socket hang up")), "x"))
      .rejects.toThrow("socket hang up");
  });

  it("substitutes the fallback when the route is gated", async () => {
    await expect(editionOptional(Promise.reject(err(404)), "fallback")).resolves.toBe("fallback");
  });

  it("passes a successful value straight through", async () => {
    await expect(editionOptional(Promise.resolve("real"), "fallback")).resolves.toBe("real");
  });

  it("gives an empty list its own type back", async () => {
    const out = await editionOptionalList<{ items: string[] }>(Promise.reject(err(404)));
    expect(out.items).toEqual([]);
  });

  it("one gated call no longer discards its siblings", async () => {
    // The exact shape of the bug: seven calls, one gated, all seven lost.
    const results = await Promise.all([
      Promise.resolve({ items: ["works"] }),
      editionOptionalList<{ items: string[] }>(Promise.reject(err(404))),
      Promise.resolve({ items: ["also works"] }),
    ]);
    expect(results[0].items).toEqual(["works"]);
    expect(results[1].items).toEqual([]);
    expect(results[2].items).toEqual(["also works"]);
  });

  it("a real outage still fails the whole feed, so apiReachable keeps meaning something", async () => {
    await expect(
      Promise.all([
        Promise.resolve({ items: [] }),
        editionOptionalList<{ items: string[] }>(Promise.reject(err(503))),
      ]),
    ).rejects.toBeTruthy();
  });
});

describe("the loaders that showed 404 on every load", () => {
  const read = (f: string) => readFileSync(resolve(__dirname, "../lib", f), "utf8");

  it("enterprise.ts gates each call so one route cannot blank the SSO cards", () => {
    const src = read("enterprise.ts");
    const block = src.slice(src.indexOf("fetchEnterpriseFeed"));
    const call = block.slice(block.indexOf("Promise.all(["), block.indexOf("]);"));
    const raw = [...call.matchAll(/^\s+client\.\w+\(/gm)];
    expect(raw, `these are still ungated: ${raw.map((m) => m[0].trim())}`).toHaveLength(0);
  });

  it("the paid marketplace loader gates its calls without masking Community module errors", () => {
    const src = read("agent-modules-paid.ts");
    expect(src).toContain("editionOptionalList(client.listHostedMarketplaceListings");
    expect(src).toContain("editionOptionalList(client.listHostedMarketplaceSyncRuns");
    // Its OWN endpoints return 200; gating them would hide a genuine failure of this feature.
    expect(read("agent-modules.ts")).toContain("client.listAgentModules(resolvedProjectId),");
  });

  it("the rule lives in one module, not copied per loader", () => {
    for (const f of ["enterprise.ts", "agent-modules-paid.ts"]) {
      expect(read(f)).toContain('from "./edition-gating"');
    }
  });
});
