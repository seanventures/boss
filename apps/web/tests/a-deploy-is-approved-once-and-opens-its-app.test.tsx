// @vitest-environment happy-dom

import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";

import { DeploysTab } from "../app/infra/deploys";
import { ResidentTile } from "../app/infra/rack";
import { gitRefOrEmpty } from "../app/infra/target-drawer";
import { deploysCadence, DEPLOY_LIVE_POLL_MS, DEPLOYS_IDLE_POLL_MS, parseDeployList, parseResident } from "../lib/infra-view";
import { deployRow, deploysFixture, mockInfraFetch, type Route } from "./_helpers/infra-fixtures";
import { allByTestId, mount, press, settle, TabHarness, type Mounted } from "./_helpers/infra-tab-render";

/**
 * "After approving the deployment … approve deploy button is still there, there is no state
 * transition to in progress … there is no link for me to even look into the App."
 *
 * Approve is one press: the row reads the answer at once and says Starting until the server's
 * state moves on. The Live row carries the app's address, which the server sets only when the
 * deploy's health check passed. The list re-reads every 3 s while a deploy is starting or
 * running, so the move to Deploying and Live is watched, not discovered.
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

describe("a deploy is approved once", () => {
  it("drops Approve and says Starting the moment the approval lands", async () => {
    const f = await open([{ method: "POST", match: /^\/v1\/decisions\/dec_wait\/approve$/, reply: () => ({ status: "approved" }) }]);
    const row = rowOf("stack:sd_wait");
    await press(row.querySelector('[data-testid="inf-deploy-row-approve"]'), "Approve");
    await settle(() => f.writes().some((w) => w.path === "/v1/decisions/dec_wait/approve"), "approve");
    await settle(() => !rowOf("stack:sd_wait").querySelector('[data-testid="inf-deploy-row-approve"]'), "approve gone");
    expect(rowOf("stack:sd_wait").querySelector('[data-testid="inf-deploy-row-reject"]')).toBeNull();
    expect(rowOf("stack:sd_wait").textContent).toContain("Starting");
    expect(rowOf("stack:sd_wait").dataset.phase).toBe("queued");
    expect(f.writes().filter((w) => w.path === "/v1/decisions/dec_wait/approve")).toHaveLength(1);
  });
});

describe("a live deploy opens its app", () => {
  it("links the app on the row the server gave an address to, and on no other", async () => {
    const live = { ...deployRow("stack:sd_live"), phase: "live", open_url: "http://edge-1:4100/",
                   sentence: "Live at http://edge-1:4100/. Health check passed: GET /healthz → 200." };
    await open([{ match: /^\/v1\/infra\/deploys$/, etag: true, reply: () => ({
      ...deploysFixture(), rows: [live, deployRow("stack:sd_wait"), deployRow("stack:sd_failed")] }) }]);
    const link = rowOf("stack:sd_live").querySelector<HTMLAnchorElement>('[data-testid="inf-deploy-row-open-app"]');
    expect(link?.getAttribute("href")).toBe("http://edge-1:4100/");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(rowOf("stack:sd_live").getAttribute("title")).toContain("Health check passed: GET /healthz → 200");
    for (const ref of ["stack:sd_wait", "stack:sd_failed"]) {
      expect(rowOf(ref).querySelector('[data-testid="inf-deploy-row-open-app"]')).toBeNull();
    }
  });
});

describe("the fleet tile fits its text and opens its app", () => {
  const CSS = readFileSync(join(__dirname, "../app/infra/infra.css"), "utf8");
  const tile = (extra: Record<string, unknown>) => parseResident({
    kind: "target", id: "t1", name: "HelpDesk", env: "staging", health: { word: "Up", tone: "green" },
    strip: [], last_deploy: { sha7: "8a009a6", at: new Date().toISOString(), word: "Live", ref: "delivery:d1" },
    url: "192.0.2.202:8000", ...extra });

  it("is not a fixed box that clips its lines", () => {
    const rule = CSS.slice(CSS.indexOf(".inf-tile {"), CSS.indexOf("}", CSS.indexOf(".inf-tile {")));
    expect(rule).toContain("height: auto");
    expect(rule).not.toMatch(/overflow:\s*hidden/);
    expect(rule).not.toMatch(/[^-]height:\s*64px/);
  });

  it("offers Open on a tile whose deploy is live and healthy, and nowhere else", () => {
    const html = (r: ReturnType<typeof tile>) =>
      renderToStaticMarkup(<ResidentTile r={r} now={Date.now()} windowEnd={null} onOpen={() => {}} />);
    const open = html(tile({ open_url: "http://192.0.2.202:8000/" }));
    expect(open).toContain('data-testid="inf-tile-open-app"');
    expect(open).toContain('href="http://192.0.2.202:8000/"');
    expect(open).toContain("8a009a6");
    expect(html(tile({ open_url: null }))).not.toContain('data-testid="inf-tile-open-app"');
  });
});

describe("the target drawer's Ref field starts as a ref", () => {
  it("never starts from the target's SSH address", () => {
    expect(gitRefOrEmpty("root@192.0.2.202:22")).toBe("");
    expect(gitRefOrEmpty("main")).toBe("main");
    expect(gitRefOrEmpty("release/1.2")).toBe("release/1.2");
    expect(gitRefOrEmpty(null)).toBe("");
  });
});

describe("the list watches a deploy that is moving", () => {
  const list = (phases: string[]) => parseDeployList({
    ...deploysFixture(), rows: phases.map((phase, i) => ({ ...deployRow("stack:sd_live"), ref: `stack:x${i}`,
                                                          phase, state: phase === "live" ? "succeeded" : "running" })) });
  it("re-reads every 3 s while one is starting or deploying, and slows when nothing moves", () => {
    expect(deploysCadence(list(["queued"]))).toBe(DEPLOY_LIVE_POLL_MS);
    expect(deploysCadence(list(["live", "deploying"]))).toBe(DEPLOY_LIVE_POLL_MS);
    const settled = parseDeployList({ ...deploysFixture(), rows: [{ ...deployRow("stack:sd_live"), phase: "live" }] });
    expect(deploysCadence(settled)).toBe(DEPLOYS_IDLE_POLL_MS);
  });
});
