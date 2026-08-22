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
      expect(codeTaskLimitFor(area)).toBe(200);
    }
  });

  it("keeps it for the default area, which renders the overview", () => {
    expect(codeTaskLimitFor(undefined)).toBe(200);
    expect(codeTaskLimitFor("")).toBe(200);
  });

  it("fetches a handful everywhere else", () => {
    for (const area of ["meetings", "discovery", "budget", "infra", "assurance", "reports"]) {
      expect(codeTaskLimitFor(area)).toBeLessThanOrEqual(12);
    }
  });

  it("cuts the dominant payload by more than an order of magnitude off-area", () => {
    expect(codeTaskLimitFor("meetings") / codeTaskLimitFor("code_tasks")).toBeLessThan(0.1);
  });

  it("200 is preserved on-area on purpose", () => {
    // BOSS's own housekeeping runs once filled the first 50 and hid every app the operator
    // had built. Lowering it everywhere would reintroduce that.
    expect(codeTaskLimitFor("code_tasks")).toBe(200);
  });
});

describe("page wiring", () => {
  it("passes the requested area to the feed", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const src = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    expect(src).toContain("fetchCodeTasksFeed(activeProjectId, requestedArea)");
  });
});
