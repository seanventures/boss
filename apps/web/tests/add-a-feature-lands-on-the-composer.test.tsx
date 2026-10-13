// @vitest-environment happy-dom
/**
 * "+ ADD A FEATURE" LANDS ON THE COMPOSER, BY CLICK (rc10.5 walk M1). The Command Center's app
 * card link navigates in-app (pushState + popstate, agents/live-util navigate). The Ask BOSS
 * hero, mounted on the same page, read ANY `?compose=` as a brief handed back from a
 * discussion and deleted it before Workflows mounted, so the operator landed on Runs with "add"
 * typed in the hero. A URL-only test cannot see that: the hero and Workflows are both mounted
 * here and the link is CLICKED.
 *
 * The class: one parameter, one reader. The hero's brief is `hero_brief=` (url-intent
 * HERO_BRIEF_PARAM, readHeroBrief); a composer command value is never a brief.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, click, mount, settle, unmount } from "./_helpers/live-render";
import { addFeaturePatch, newProjectPatch } from "../lib/compose-links";
import { HERO_BRIEF_PARAM, readHeroBrief } from "../app/url-intent";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
                                    usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));
vi.mock("../app/workflows/new-project-composer", () => ({
  NewProjectComposer: (p: { mode: string | null; appKey?: string | null }) => (
    <div data-testid="np-stub" data-mode={p.mode ?? ""} data-app={p.appKey ?? ""} />
  ),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AskBossHero } = await import("../app/ask-boss-hero");
const { DeliveryWorkflows } = await import("../app/delivery-workflows-ui");
const { Go } = await import("../app/command-center/hero");

const RUN = { id: "wfr_1", status: "running", workflow_key: "w", workflow_name: "W", version: 1, step_count: 1,
              steps_done: 0, current_step: "build", waiting_on: null, blocked_reason: null, error: null,
              updated_at: null, input: { title: "T" }, finished: false, standing: "active", actions: [], steps: [] };
const VIEWS: Record<string, unknown> = {
  capabilities: { capabilities: [], manual_approval_risk_floor: 2 }, catalogue: { items: [] }, installed: { items: [] },
  runs: { runs: [RUN] }, custom_steps: { steps: [] }, schedules: { schedules: [] },
};

beforeEach(() => {
  window.history.replaceState(null, "", "/?org=org_1&area=overview");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://console.test");
    return Response.json(VIEWS[url.searchParams.get("view") ?? ""] ?? {});
  }));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

/** The Command Center's hero and an app card's link, with Workflows mounted beside them (the
 *  shell keeps areas mounted), the hero first so its listener hears popstate first. */
function Page() {
  return (
    <>
      <AskBossHero projectId="p1" actor="ceo" orgId="org_1" deploymentAvailable={false} layout="hero" />
      <Go patch={addFeaturePatch("helpdesk")} orgId="org_1" testId="cc-app-add">+ Add a feature</Go>
      <Go patch={newProjectPatch()} orgId="org_1" testId="cc-new">New build</Go>
      <DeliveryWorkflows projectId="p1" actor="ceo" />
    </>
  );
}
const params = () => new URLSearchParams(window.location.search);
const heroBox = () => document.querySelector<HTMLTextAreaElement>(".askboss textarea");

describe("an app card's + Add a feature, clicked", () => {
  it("opens New project in Add a feature mode on that app, and the hero keeps its box", async () => {
    await mount(<Page />);
    await settle();
    await click($("[data-testid=cc-app-add]"));
    await settle();
    expect($("[data-testid=dw-tab-new]")?.getAttribute("aria-selected")).toBe("true");
    expect($("[data-testid=np-stub]")?.dataset.mode).toBe("add");
    expect($("[data-testid=np-stub]")?.dataset.app).toBe("helpdesk");
    expect(heroBox()?.value ?? "").toBe("");
    expect([params().get("area"), params().get("tab"), params().get("compose")])
      .toEqual(["delivery_workflows", "new", null]);
  });

  it("New build does the same in New app mode", async () => {
    await mount(<Page />);
    await settle();
    await click($("[data-testid=cc-new]"));
    await settle();
    expect($("[data-testid=np-stub]")?.dataset.mode).toBe("new");
    expect(heroBox()?.value ?? "").toBe("");
  });

  it("Back returns to the Command Center", async () => {
    await mount(<Page />);
    await settle();
    await click($("[data-testid=cc-app-add]"));
    await settle();
    window.history.back();
    await new Promise((r) => setTimeout(r, 20));
    await settle();
    expect(params().get("area")).toBe("overview");
  });
});

describe("the hero takes only its own brief", () => {
  it("a brief handed back from a discussion fills the box and is consumed", async () => {
    window.history.replaceState(null, "", `/?org=org_1&area=overview&${HERO_BRIEF_PARAM}=A+shift+swap+board`);
    await mount(<Page />);
    await settle();
    expect(heroBox()?.value).toBe("A shift swap board");
    expect(params().get(HERO_BRIEF_PARAM)).toBeNull();
    expect(params().get("org")).toBe("org_1");
  });

  it("never reads a composer command, under either name", () => {
    for (const cmd of ["new", "add", "template"]) {
      expect(readHeroBrief(`?compose=${cmd}`)).toBeNull();
      expect(readHeroBrief(`?${HERO_BRIEF_PARAM}=${cmd}`)).toBeNull();
    }
    expect(readHeroBrief("?compose=Build+me+a+CRM")).toBeNull();
    expect(readHeroBrief(`?a=1&${HERO_BRIEF_PARAM}=Build+me+a+CRM`))
      .toEqual({ brief: "Build me a CRM", nextSearch: "?a=1" });
  });
});
