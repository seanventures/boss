// @vitest-environment happy-dom
/**
 * Walk D1, D4, D9: a press in the cockpit answers at once. Stop says `Stopped by you` and
 * `Build stopped` before the API answers (it can wait on the worker); Steer clears its box and
 * says `Guidance queued`; Credits credit THIS build's acts and never open by themselves.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, click, mount, rerender, settle, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit, cockpitPrOpen } from "./_helpers/factory-fixtures";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span data-testid="live-deploy" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Cockpit } = await import("../app/factory/cockpit");

let toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
const view = () => <Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />;
const text = (sel: string) => $(sel)?.textContent ?? "";

/** The chain a build's acts route answers: five agents with signed work. */
const CHAIN = {
  run_id: "crun_inv2", acts: ["chief_architect", "qa", "software_engineer", "adversarial_reviewer", "security_reviewer"]
    .map((r, i) => ({ act_id: `act_${i}0000000000001`, role_key: r, kind: "stage", status: "complete",
                      root_act_id: "act_ROOT", handed_by_kind: "boss", how: "next_stage", edge_label: "Next stage",
                      label: `Stage ${i}`, accountable: "ceo", started_at: "2026-10-04T09:12:00.000Z",
                      links: {}, artifacts: [], considered: [], scope: {} })),
  edges: [],
};

beforeEach(() => { toasts = []; window.addEventListener("boss:toast", onToast); });
afterEach(() => { window.removeEventListener("boss:toast", onToast); unmount(); vi.unstubAllGlobals(); });

describe("the cockpit answers at once", () => {
  it("D1 Stop: Stopped by you and Build stopped before the API answers", async () => {
    stubFetch(cockpit());
    const inner = globalThis.fetch;
    // The cancel never answers while the test looks: the worker holds the row.
    vi.stubGlobal("fetch", vi.fn((input: unknown, init?: RequestInit) =>
      String(input).includes("/cancel") ? new Promise(() => {}) : inner(input as RequestInfo, init)));
    await mount(view());
    await click($('[data-testid="fx-stop"]'));
    await click($('[data-testid="fx-stop-yes"]'));
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("Stopped by you");
    expect(toasts).toContain("Build stopped");
    expect($('[data-testid="fx-stop"]')).toBeNull();
    expect($('[data-testid="fx-cockpit"]')?.dataset.still).toBe("true");
  });

  it("D1 Stop refused: the press is undone and the API's sentence shown", async () => {
    stubFetch(cockpit(), (url, init) => (init?.method === "POST" && url.includes("/cancel")
      ? new Response(JSON.stringify({ detail: "not yours to stop" }), { status: 403 }) : undefined));
    await mount(view());
    await click($('[data-testid="fx-stop"]'));
    await click($('[data-testid="fx-stop-yes"]'));
    await settle();
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("Coding");
    expect(text('[data-testid="fx-error"]')).toBe("not yours to stop");
  });

  it("D1 a Stop the build outran says so, and the build reads as what it is", async () => {
    const body = cockpitPrOpen();
    (body.story as Record<string, unknown>[]).push({ type: "you", id: "stopped", station: "pr", what: "stopped",
      actor: "ceo", word: "Too late · it had finished", at: "2026-10-04T09:30:00.000Z" });
    stubFetch(body);
    await mount(view());
    expect($('[data-testid="fx-state"]')?.textContent).toContain("PR open");
    expect($('[data-testid="fx-you-word-stopped"]')?.textContent).toBe("Too late · it had finished");
  });

  it("D9 Steer: Guidance queued, and the box is empty and closed", async () => {
    stubFetch(cockpit());
    await mount(view());
    await click($('[data-testid="fx-steer"]'));
    const input = $('[data-testid="fx-steer-input"]') as HTMLInputElement;
    const { act } = await import("react");
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "Use the shared CSV writer");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click($('[data-testid="fx-steer-box"] button[type="submit"]'));
    expect(toasts).toContain("Guidance queued");
    expect($('[data-testid="fx-steer-box"]')).toBeNull();
    await click($('[data-testid="fx-steer"]'));
    expect(($('[data-testid="fx-steer-input"]') as HTMLInputElement).value).toBe("");
  });

  it("D4 Credits: this build's acts, a real button, never opened by itself", async () => {
    const chainUrls: string[] = [];
    let body = cockpit();
    stubFetch(() => body, (url) => {
      if (url.includes("/api/run-acts") && url.includes("view=chain")) { chainUrls.push(url); return CHAIN; }
      return undefined;
    });
    await mount(view());
    expect(chainUrls.some((u) => u.includes("run_id=crun_inv2"))).toBe(true);
    expect(chainUrls.some((u) => u.includes("wfr_"))).toBe(false);
    const btn = $('[data-testid="ag-credits-open"]');
    expect(btn?.className).toContain("ag-btn");
    // The build ships while the cockpit is open: the Credits dialog stays shut.
    body = { ...cockpitPrOpen(), run: { ...(cockpitPrOpen().run as object), id: "crun_inv2" } };
    await rerender(view());
    const { act } = await import("react");
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await settle();
    expect(document.querySelector('[role="dialog"][aria-label*="Credits"], .ag-credits-dialog')).toBeNull();
  });

  it("D19: a face's Act card opens on <body>, beside the face, not inside a transformed box", async () => {
    stubFetch(cockpit());
    await mount(view());
    const face = $('[data-testid="fx-story-face-act_TESTS0000000001"]')!;
    face.getBoundingClientRect = () => ({ left: 300, right: 324, top: 400, bottom: 424, width: 24, height: 24, x: 300, y: 400, toJSON: () => ({}) }) as DOMRect;
    await click(face);
    await settle();
    const pop = $('[data-testid="fx-act-pop"]')!;
    expect(pop.parentElement).toBe(document.body);
    expect(Number.parseInt(pop.style.top, 10)).toBeGreaterThanOrEqual(424);
  });
});
