/**
 * The console was DDoSing its own API.
 *
 * Measured on goldeneye: `/v1/code-tasks?limit=200` returns **1.41 MB**, while every other feed
 * on the page combined is ~130 KB — 91% of the payload for one list. `page.tsx` fetches it on
 * EVERY area and renders it across ~30 sections regardless of `?area=`.
 *
 * One page load took 111 seconds and starved the API's healthcheck twice (`/health` returned
 * 000 at 19:07:21 and 19:08:59). Docker marked the container unhealthy, Traefik evicted it, and
 * the console's own fetches got Traefik's 404 — surfacing as "HTTP 404 / No hosts yet" about a
 * host that was registered and verified, and as "API unreachable" all day.
 */
import { describe, expect, it } from "vitest";
import { codeTaskLimitFor } from "../lib/code-tasks";

describe("codeTaskLimitFor", () => {
  it("keeps the full list where the list is actually rendered", () => {
    for (const area of ["code_tasks", "delivery", "overview"]) {
      expect(codeTaskLimitFor(area)).toBe(600);
    }
  });

  it("keeps it for the default area, which renders the overview", () => {
    expect(codeTaskLimitFor(undefined)).toBe(600);
    expect(codeTaskLimitFor("")).toBe(600);
  });

  it("fetches a handful everywhere else", () => {
    for (const area of ["meetings", "discovery", "budget", "infra", "assurance", "reports"]) {
      expect(codeTaskLimitFor(area)).toBeLessThanOrEqual(12);
    }
  });

  it("cuts the dominant payload by more than an order of magnitude off-area", () => {
    expect(codeTaskLimitFor("meetings") / codeTaskLimitFor("code_tasks")).toBeLessThan(0.1);
  });

  it("covers every task on-area, because 200 did not", () => {
    // The cap above was set to bound a payload, and the bound was measured wrong. On goldeneye,
    // with 244 real tasks:
    //     limit=200 -> 1,481,518 bytes in 0.885s   (44 invisible)
    //     limit=400 -> 1,707,102 bytes in 1.025s   (all of them)
    // Showing everything costs 225 KB and 0.14s. That is what the cap bought, and it was paid
    // for with "not all projects are getting retrieved under the Factory", reported repeatedly
    // — because a list that silently stops at 200 looks exactly like a project that has 200.
    expect(codeTaskLimitFor("code_tasks")).toBeGreaterThanOrEqual(500);
  });

  it("still cuts the payload hard off-area", () => {
    // The 111-second page load was caused by fetching this feed on EVERY area and rendering it
    // across ~30 sections, not by the on-area count. That fix stays exactly as it was.
    expect(codeTaskLimitFor("meetings")).toBeLessThanOrEqual(12);
  });
});

describe("page wiring", () => {
  it("passes the requested area to the feed", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const src = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    // The property, not the formatting: the area must reach the fetcher, because that is what
    // picks the limit above. The call also carries an `active` gate now, so pinning the exact
    // one-line form broke on a reflow that changed nothing about this guarantee.
    const call = /fetchCodeTasksFeed\(([^)]*)\)/s.exec(src)?.[1];
    expect(call).toBeTruthy();
    expect(call).toContain("activeProjectId");
    expect(call).toContain("requestedArea");
  });
});
