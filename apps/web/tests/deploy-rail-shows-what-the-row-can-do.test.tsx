// @vitest-environment happy-dom

import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { DeploysTab } from "../app/infra/deploys";
import { deployRow, deploysFixture, mockInfraFetch, type Route } from "./_helpers/infra-fixtures";
import { allByTestId, byTestId, mount, press, settle, TabHarness, type Mounted } from "./_helpers/infra-tab-render";

/**
 * Infra › Deploys: a row offers exactly what its `can` says — Approve/Reject on the deploy
 * waiting for you, `Roll back` only on the newest failed deploy that has an earlier good one,
 * nothing on a finished one — and its step dots say each step's state with a glyph, not colour
 * alone. The words and `can` are the server's; the rail decides nothing.
 */

let m: Mounted | null = null;
let restore: (() => void) | null = null;
afterEach(async () => { await m?.unmount(); m = null; restore?.(); restore = null; });

async function open(routes: Route[] = []) {
  const f = mockInfraFetch({ routes });
  restore = f.install();
  m = await mount(<TabHarness Tab={DeploysTab} />, "?area=infra&view=deploys");
  await settle(() => allByTestId("inf-deploy-row").length > 0, "rail");
  return f;
}
const rowOf = (ref: string) => document.querySelector<HTMLElement>(`[data-testid="inf-deploy-row"][data-ref="${ref}"]`)!;
/** The row's actions (its name and ↗ are not actions). */
const labels = (el: Element) => [...el.querySelectorAll(".inf-dep-act button")].map((b) => b.textContent?.trim());

describe("the deploy rail", () => {
  it("draws every deploy, grouped by day, with the server's word", async () => {
    await open();
    expect(allByTestId("inf-deploy-row")).toHaveLength((deploysFixture().rows as unknown[]).length);
    const heads = [...document.querySelectorAll(".inf-rail-day-head")].map((h) => h.textContent);
    expect(heads[0]).toBe("TODAY");
    expect(heads).toContain("YESTERDAY");
    expect(rowOf("stack:sd_wait").textContent).toContain("Needs you");
    expect(rowOf("stack:sd_rb").textContent).toContain("↩ e1f2a3b");
    expect(rowOf("stack:sd_live").textContent).toContain("1m 52s");
  });

  it("offers Approve/Reject on the waiting deploy, Roll back on the newest failed one, nothing on a finished one", async () => {
    await open();
    expect(labels(rowOf("stack:sd_wait"))).toEqual(expect.arrayContaining(["Approve", "Reject"]));
    expect(labels(rowOf("stack:sd_wait"))).not.toContain("Roll back");
    expect(labels(rowOf("stack:sd_failed"))).toContain("Roll back");
    expect(allByTestId("inf-deploy-rollback")).toHaveLength(1);
    for (const ref of ["stack:sd_live", "stack:sd_ok", "stack:sd_rb", "registry:dr_1"]) {
      expect(labels(rowOf(ref))).toEqual([]);
    }
  });

  it("shows no Roll back when the server says the failed deploy has nothing to go back to", async () => {
    const failedNoPrior = { ...deployRow("stack:sd_failed"), can: { approve: false, reject: false, rollback: false, redeploy: true } };
    await open([{ match: /^\/v1\/infra\/deploys$/, etag: true, reply: () => ({
      ...deploysFixture(), rows: [failedNoPrior, deployRow("stack:sd_live")] }) }]);
    expect(labels(rowOf("stack:sd_failed"))).not.toContain("Roll back");
    expect(byTestId("inf-deploy-rollback")).toBeNull();
  });

  it("step dots say done, running and failed as glyphs, and only a running step moves", async () => {
    await open();
    const failed = [...rowOf("stack:sd_failed").querySelectorAll(".inf-step")];
    expect(failed.map((s) => s.getAttribute("data-s"))).toEqual(["done", "done", "done", "failed", "pending", "pending"]);
    expect(failed[3].textContent).toBe("✗");
    expect(failed[3].getAttribute("title")).toBe("Migrate, failed");
    const running = rowOf("stack:sd_run").querySelector('.inf-step[data-s="running"]')!;
    expect(running.textContent).toBe("◐");
    expect(running.hasAttribute("data-anim")).toBe(true);
    expect(document.querySelectorAll("[data-anim]").length).toBeLessThanOrEqual(8);
  });

  it("Approve goes through the decisions API with the row's decision; Roll back through the routed rollback", async () => {
    const f = await open();
    await press(rowOf("stack:sd_wait").querySelector('[data-testid="inf-deploy-row-approve"]'), "Approve");
    await settle(() => f.writes().some((w) => w.path === "/v1/decisions/dec_wait/approve"), "approve");
    expect(f.writes().find((w) => w.path === "/v1/decisions/dec_wait/approve")?.body).toMatchObject({ actor: "ceo" });

    await press(rowOf("stack:sd_failed").querySelector('[data-testid="inf-deploy-rollback"]'), "Roll back");
    await settle(() => f.writes().some((w) => /\/v1\/infra\/deploys\/stack(%3A|:)sd_failed\/rollback$/.test(w.path)), "rollback");
    expect(f.writes().find((w) => w.path.endsWith("/rollback"))?.body).toEqual({ project_id: "project_1", actor: "ceo" });
  });

  it("a chip filters through the URL and the server", async () => {
    const f = await open();
    await press(byTestId("inf-deploys-chip-failed"), "Failed chip");
    await settle(() => allByTestId("inf-deploy-row").length === 1, "filtered");
    expect(new URLSearchParams(window.location.search).get("state")).toBe("failed");
    expect(f.gets(/^\/v1\/infra\/deploys$/).some((c) => c.url.includes("state=failed"))).toBe(true);
  });

  it("Show older appends the next page by the server's cursor", async () => {
    const f = await open();
    await press(byTestId("inf-deploys-older"), "Show older");
    await settle(() => !!rowOf("stack:sd_older"), "older page");
    expect(f.gets(/^\/v1\/infra\/deploys$/).some((c) => c.url.includes("cursor=cur_2"))).toBe(true);
    expect(byTestId("inf-deploys-older")).toBeNull();
  });

  it("says a newer failed attempt beside the Live deploy, and opens it (decision 2026-10-04)", async () => {
    const live = { ...deployRow("stack:sd_live"), failed_after: { at: new Date(Date.now() - 2 * 3600_000).toISOString(), ref: "stack:sd_failed" } };
    const rows = (deploysFixture().rows as Record<string, unknown>[]).map((r) => (r.ref === "stack:sd_live" ? live : r));
    await open([{ match: /^\/v1\/infra\/deploys$/, reply: () => ({ ...deploysFixture(), rows }) }]);
    const row = rowOf("stack:sd_live");
    expect(row.textContent).toContain("Live");
    const note = row.querySelector<HTMLButtonElement>("[data-testid='inf-deploy-failed-after']")!;
    expect(note.textContent).toBe("Last attempt failed, 2h ago");
    // Only the Live row says it.
    expect(document.querySelectorAll("[data-testid='inf-deploy-failed-after']")).toHaveLength(1);
    await press(note);
    expect(new URLSearchParams(window.location.search).get("deploy")).toBe("stack:sd_failed");
  });
});
