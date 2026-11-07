// @vitest-environment happy-dom
/**
 * PROJECT EXPERIENCE, ROUND TWO (BOSSAPP-913/914): the pieces the walk found missing.
 *
 * - 07: each card's detector node is the wireframe's name with its icon, violet ("decision"):
 *   "Incident hotspot" (detective), "Alert gap" (bell-slash) → "Add alert" by Beacon.
 * - Costs said one way: "$2.10" for money, "≈ $2.10" + "plan" on a subscription, never "$0.00".
 * - 05-32: Code Tasks' build rows draw the Project's own station strip (`fromFactory`).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { $, $$, mountAs, unmount } from "./_helpers/multirepo-fixtures";
import { parseRec } from "../lib/ambient";
import { costWords } from "../lib/project-experience";
import type { Station } from "../lib/factory-floor";

const { recFlow } = await import("../app/projects/tab-roadmap");
const { Flow, Cost } = await import("../app/projects/kit");
const { Stations, fromFactory } = await import("../app/stations");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => unmount());

const rec = (detector: string, label: string, icon: string, extra: Record<string, unknown> = {}) => parseRec({
  id: `rec_${detector}`, title: "x", kind: "hardening", detector, proposed_shape: "ticket", status: "open",
  badges: { kind: "Hardening" }, squad: detector === "alert_gap" ? ["sre"] : ["engineering_captain"],
  strip: [{ type: "signal", label: detector === "alert_gap" ? "Live app" : "Incidents", badges: ["×2"] },
          { type: "agent", label, icon, badges: [] }, { type: "action", label: "x", badges: [] }], ...extra,
});

describe("Roadmap detector nodes (07-09, 07-12)", () => {
  it("names the detector as the wireframe does, violet, with its own icon", async () => {
    await mountAs(<div>
      <Flow testid="a" nodes={recFlow(rec("incident_cause", "Incident hotspot", "detective"))} />
      <Flow testid="b" nodes={recFlow(rec("alert_gap", "Alert gap", "bell-slash"))} />
    </div>);
    const a = $$("[data-testid='a'] [role='listitem']");
    expect(a.map((n) => n.textContent)).toEqual(["2 incidents", "Incident hotspot", "Test + fix"]);
    expect(a[1].getAttribute("data-t")).toBe("decision");
    expect(a[1].querySelector("svg")).not.toBeNull();
    const b = $$("[data-testid='b'] [role='listitem']");
    expect(b.map((n) => n.textContent)).toEqual(["2 live app", "Alert gap", "Add alert"]);
    expect(b[2].querySelector(".ag-avatar")?.getAttribute("data-role-key") ?? "sre").toContain("sre");
  });
});

describe("costs, one way everywhere", () => {
  it("money is dollars; a subscription is ≈ list price with the plan marker; nothing is no chip", async () => {
    expect(costWords(2.1, null)).toMatchObject({ text: "$2.10", plan: false });
    expect(costWords(0, 57.55)).toMatchObject({ text: "≈ $57.55", plan: true });
    expect(costWords(0, 57.55)!.title).toContain("On subscription");
    expect(costWords(0, null)).toBeNull();
    await mountAs(<div><Cost usd={0} list={3.4} testid="c1" /><Cost usd={0} list={null} testid="c2" /><Cost usd={1.2} list={0.5} testid="c3" /></div>);
    expect($("[data-testid='c1']")!.textContent).toBe("≈ $3.40plan");
    expect($("[data-testid='c2']")).toBeNull();
    expect($("[data-testid='c3']")!.textContent).toBe("$1.20");
    expect(document.body.textContent).not.toContain("$0.00");
  });
});

describe("one station strip (05-32)", () => {
  const st = (key: Station["key"], status: Station["status"], roleKey: string | null = null): Station => ({
    key, label: key, status, face: roleKey ? "agent" : "boss", roleKey, hollowRole: null, actId: null, actIds: [], seats: [],
    word: status, rounds: 1, links: [], loopWith: null,
  } as unknown as Station);
  it("maps a build's stations onto the six the Project's Work rows draw", async () => {
    const six = fromFactory([st("plan", "done", "product_manager"), st("tests", "done"), st("build", "working", "software_engineer"),
                             st("check", "pending"), st("review", "pending"), st("pr", "pending"), st("preview", "pending"), st("ship", "pending")],
                            (r) => ({ product_manager: "Juno", software_engineer: "Forge" } as Record<string, string>)[r] ?? r);
    expect(six.map((s) => `${s.label}:${s.state}`)).toEqual(["P:done", "D:skipped", "B:now", "R:todo", "PR:todo", "S:todo"]);
    expect(six[2].who).toContain("Forge");
    await mountAs(<Stations stages={six} testid="s" />);
    expect($$("[data-testid='s'] .px-st-n").map((n) => n.textContent)).toEqual(["P", "D", "B", "R", "PR", "S"]);
    expect($("[data-testid='s'] [data-station='build']")!.getAttribute("title")).toBe("Build: in progress · BOSS, Forge");
  });
});

describe("Overview cards load or fail on their own (04 states)", () => {
  it("a card BOSS couldn't read says Couldn't load · Retry inside itself; the others still show", async () => {
    const { FX_OVERVIEW } = await import("../lib/project-experience-fixtures");
    const { parseOverview } = await import("../lib/project-experience");
    const { OverviewTab } = await import("../app/projects/tab-overview");
    const ov = parseOverview({ ...FX_OVERVIEW, errors: { tile_cost: "Couldn't load (OperationalError)", needs_you: "Couldn't load", captain: "x" } });
    const refreshed: string[] = [];
    const ctx = { projectId: "helpdesk", actor: "ceo", orgId: null, overview: ov, demo: false, work: null, workState: "ready" as const,
                  refreshWork: () => undefined, refreshOverview: () => refreshed.push("overview"), onboarding: null,
                  tab: () => undefined, open: () => undefined, ask: () => undefined };
    await mountAs(<OverviewTab ctx={ctx} />);
    expect($("[data-testid='px-tile-cost'] [data-testid='px-card-failed-tile_cost']")!.textContent).toContain("Couldn't load");
    expect($("[data-testid='px-ov-needs'] [data-testid='px-card-failed-needs_you']")).not.toBeNull();
    expect($("[data-testid='px-ov-captain'] [data-testid='px-card-failed-captain']")).not.toBeNull();
    // Every other card still answers.
    expect($("[data-testid='px-tile-work'] [data-testid^='px-card-failed']")).toBeNull();
    expect($("[data-testid='px-tile-staging']")!.textContent).not.toContain("Couldn't load");
    expect($("[data-testid='px-ov-next']")).not.toBeNull();
    // Retry inside a tile is never a nested button, and reads the Overview again.
    const retry = $("[data-testid='px-card-retry-tile_cost']")!;
    expect(retry.tagName).toBe("SPAN");
    retry.click();
    expect(refreshed).toEqual(["overview"]);
  });
});
