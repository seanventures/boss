// @vitest-environment happy-dom

import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { PreviewsTab, ttlOf } from "../app/infra/previews";
import { mockInfraFetch, previewsFixture, type Route } from "./_helpers/infra-fixtures";
import { allByTestId, byTestId, mount, press, settle, TabHarness, type Mounted } from "./_helpers/infra-tab-render";

/**
 * Infra › Previews says what is running and until when — the time left as a bar of the time it
 * was given, amber in its last five minutes — and each row offers only what its `can` allows:
 * Stop (POST /v1/previews/{id}/stop, no confirm, toast `Preview stopped`), Restart (the routed
 * POST /v1/infra/previews/{id}/restart), Log, Open, Build.
 */

let m: Mounted | null = null;
let restore: (() => void) | null = null;
afterEach(async () => { await m?.unmount(); m = null; restore?.(); restore = null; });

async function open(routes: Route[] = [], search = "?area=infra&view=previews") {
  const f = mockInfraFetch({ routes });
  restore = f.install();
  m = await mount(<TabHarness Tab={PreviewsTab} />, search);
  await settle(() => !!byTestId("inf-previews"), "previews");
  return f;
}
const rowOf = (id: string) => document.querySelector<HTMLElement>(`[data-testid="inf-preview-row"][data-id="${id}"]`);

describe("the previews tab", () => {
  it("the time left is the share of the time given, and its words", () => {
    const now = Date.parse("2026-10-04T12:00:00Z");
    const t = ttlOf({ createdAt: "2026-10-04T11:00:00Z", expiresAt: "2026-10-04T12:30:00Z" }, now)!;
    expect(t.fraction).toBeCloseTo(1 / 3);
    expect(t.label).toBe("30m left");
    expect(ttlOf({ createdAt: null, expiresAt: null }, now)).toBeNull();
  });

  it("LIVE rows show the TTL, amber under five minutes; FAILED keeps its failure word; ENDED waits behind Show", async () => {
    await open();
    expect(allByTestId("inf-preview-row")).toHaveLength(3);
    const live = rowOf("pv_live")!;
    expect(live.textContent).toContain("search-revamp");
    expect(live.textContent).toContain("Docker");
    expect(live.textContent).toContain(":4127");
    const ttl = live.querySelector('[data-testid="inf-preview-ttl"]')!;
    expect(ttl.textContent).toMatch(/^4[01]m left$/);
    expect(ttl.getAttribute("data-tone")).toBeNull();
    expect(ttl.getAttribute("title")).toMatch(/^Expires at /);
    const fill = Number(ttl.querySelector(".inf-ttl-fill")!.getAttribute("width"));
    expect(fill).toBeGreaterThan(60);
    expect(fill).toBeLessThanOrEqual(70);

    const starting = rowOf("pv_start")!.querySelector('[data-testid="inf-preview-ttl"]')!;
    expect(starting.getAttribute("data-tone")).toBe("amber");
    expect(starting.textContent).toMatch(/^[34]m left$/);

    expect(rowOf("pv_fail")!.textContent).toContain("Failed · Port never opened");
    expect(rowOf("pv_fail")!.querySelector('[data-testid="inf-preview-ttl"]')).toBeNull();
    expect(rowOf("pv_end")).toBeNull();
    await press(byTestId("inf-previews-show-ended"), "Show");
    expect(rowOf("pv_end")).not.toBeNull();
  });

  it("offers only what each row's can allows", async () => {
    await open();
    const has = (id: string, t: string) => !!rowOf(id)!.querySelector(`[data-testid="${t}"]`);
    expect(has("pv_live", "inf-preview-open")).toBe(true);
    expect(has("pv_live", "inf-preview-stop")).toBe(true);
    expect(has("pv_start", "inf-preview-open")).toBe(false);
    expect(has("pv_start", "inf-preview-restart")).toBe(false);
    expect(has("pv_fail", "inf-preview-stop")).toBe(false);
    expect(has("pv_fail", "inf-preview-restart")).toBe(true);
    expect(rowOf("pv_live")!.querySelector('[data-testid="inf-preview-build"]')?.getAttribute("href"))
      .toBe("?area=code_tasks&build=run_search_1");
    expect(rowOf("pv_live")!.querySelector('[data-testid="inf-preview-stop"]')?.getAttribute("title"))
      .toBe("Stops the preview now. Start it again from the build.");
  });

  it("Stop posts the preview's stop, with no confirm; Restart posts the routed restart", async () => {
    const f = await open();
    const toasts: string[] = [];
    const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
    window.addEventListener("boss:toast", onToast);
    try {
      await press(rowOf("pv_live")!.querySelector('[data-testid="inf-preview-stop"]'), "Stop");
      await settle(() => f.writes().some((w) => w.path === "/v1/previews/pv_live/stop"), "stop");
      expect(f.writes().find((w) => w.path === "/v1/previews/pv_live/stop")?.body).toEqual({ actor: "ceo" });
      await settle(() => toasts.includes("Preview stopped"), "toast");

      await press(rowOf("pv_fail")!.querySelector('[data-testid="inf-preview-restart"]'), "Restart");
      await settle(() => f.writes().some((w) => w.path === "/v1/infra/previews/pv_fail/restart"), "restart");
      expect(f.writes().find((w) => w.path === "/v1/infra/previews/pv_fail/restart")?.body)
        .toEqual({ project_id: "project_1", actor: "ceo" });
    } finally {
      window.removeEventListener("boss:toast", onToast);
    }
  });

  it("Restart is disabled, with the reason, when the preview's build no longer exists", async () => {
    await open([{ match: /^\/v1\/infra\/previews$/, etag: true, reply: () => {
      const f = previewsFixture() as { failed_24h: Record<string, unknown>[] };
      f.failed_24h[0] = { ...f.failed_24h[0], can: { ...(f.failed_24h[0].can as object), restart: false },
                          restart_why: "Its build no longer exists, so it cannot start again" };
      return f;
    } }]);
    const btn = rowOf("pv_fail")!.querySelector<HTMLButtonElement>('[data-testid="inf-preview-restart"]')!;
    expect(btn).not.toBeNull();
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe("Its build no longer exists, so it cannot start again");
  });

  it("says Nothing running when no preview is live, and keeps FAILED", async () => {
    await open([{ match: /^\/v1\/infra\/previews$/, etag: true, reply: () => ({
      ...previewsFixture(), counts: { live: 0, failed_24h: 1, ended_24h: 1 }, live: [] }) }]);
    expect(byTestId("inf-previews-none")?.textContent).toBe("Nothing running");
    expect(rowOf("pv_fail")).not.toBeNull();
  });

  it("Log opens the preview's log drawer in the URL and reads its log once it has ended", async () => {
    const f = await open([], "?area=infra&view=previews");
    await press(rowOf("pv_fail")!.querySelector('[data-testid="inf-preview-log-btn"]'), "Log");
    expect(new URLSearchParams(window.location.search).get("preview")).toBe("pv_fail");
    await settle(() => !!byTestId("inf-preview-log-text"), "log");
    expect(byTestId("inf-preview-log-text")?.textContent).toContain("ready - started server");
    expect(f.gets(/^\/v1\/previews\/pv_fail\/logs$/)).toHaveLength(1);
  });
});
