// @vitest-environment happy-dom
/**
 * The app drawer (spec §4.9): an app serving a build other than the one last deployed says so
 * in an amber `Serving <v>` chip — because the server said `serving_mismatch`, never because the
 * web compared strings; an ongoing incident reads `Ongoing` with the deploy just before it; and
 * `Probe now` probes THIS app only, once per press, then says `Probed · <word>`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { TOAST_EVENT } from "../lib/api-write";
import { appWire } from "./_helpers/obs-fixtures";
import { $, $$, click, settle, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the app drawer", () => {
  it("flags a build mismatch with the build the app reports", async () => {
    await openObs("?view=apps&app=t_live");
    const chip = $("[data-testid='obs-app-serving']");
    expect(text(chip)).toBe("Serving 9f1e2d3");
    expect(chip?.getAttribute("title")).toBe("The app reports a different build than the last deploy");
    expect(chip?.getAttribute("data-tone")).toBe("amber");
    const facts = text($("[data-testid='obs-app-facts']"));
    expect(facts).toContain("Uptime 24h99.90%");
    expect(facts).toContain("Builda1b2c3d");
  });

  it("says nothing when the app serves what was deployed", async () => {
    await openObs("?view=apps&app=t_down");
    expect($("[data-testid='obs-app-facts']")).not.toBeNull();
    expect($("[data-testid='obs-app-serving']")).toBeNull();
  });

  it("only the server's word counts: equal-looking versions with no mismatch flag draw no chip", async () => {
    await openObs("?view=apps&app=t_live", { app: (id) => appWire(id, { facts: { ...appWire(id).facts, serving_mismatch: false } }) });
    expect($("[data-testid='obs-app-serving']")).toBeNull();
  });

  it("an ongoing incident reads Ongoing, with its error behind Show error and the deploy before it", async () => {
    await openObs("?view=apps&app=t_down");
    const inc = $("[data-testid='obs-app-incidents']")!;
    expect($$("[data-testid='obs-incident-ongoing']")).toHaveLength(1);
    const link = inc.querySelector("[data-testid='obs-incident-deploy']");
    expect(link?.getAttribute("href")).toBe("?area=infra&view=deploys&deploy=stack%3Asd_failed");
    expect(inc.querySelector(".obs-errfull")).toBeNull();
    await click(inc.querySelector("[data-testid='obs-incident-show-error']"));
    expect(text(inc.querySelector(".obs-errfull"))).toContain("ECONNREFUSED");
    expect(text($("[data-testid='obs-app-dashboard-why']"))).toBe("No dashboard");
    expect($("[data-testid='obs-app-logs']")?.getAttribute("href")).toBe("?area=activity&src=deployment&subject=t_down");
  });

  it("Probe now probes this app only, once per press, and says what it found", async () => {
    const toasts: string[] = [];
    const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
    window.addEventListener(TOAST_EVENT, onToast);
    const rec = await openObs("?view=apps&app=t_down", { writes: { "/probe": { body: { word: "Down", tone: "red", checked_at: "x" } } } });
    const pulseGets = () => rec.calls.filter((u) => u.startsWith("/v1/observability/apps?")).length;
    const before = pulseGets();
    const btn = $("[data-testid='obs-probe-now']")!;
    btn.click();
    btn.click();
    await settle();
    window.removeEventListener(TOAST_EVENT, onToast);
    expect(rec.writes).toHaveLength(1);
    expect(rec.writes[0].url).toBe("/v1/observability/apps/t_down/probe");
    expect(rec.writes[0].body).toEqual({ project_id: "p1", actor: "ceo" });
    expect(toasts).toContain("Probed · Down");
    expect(pulseGets()).toBeGreaterThan(before);
  });

  it("closing it drops app= from the URL", async () => {
    await openObs("?view=apps&app=t_live");
    await click($("[data-testid='obs-app-drawer-close']"));
    expect(params().get("app")).toBeNull();
    expect($("[data-testid='obs-app-drawer']")).toBeNull();
  });
});
