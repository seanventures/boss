// @vitest-environment happy-dom
/**
 * Walk D2, D12, D13, D14, D17, D21, D22: the Floor is one source and reads at a glance.
 * A workflow gate on a build's own run is a NEEDS YOU card counted like the pill; BOSS's own
 * builds are builds (with a tag); J/K work without a click first and Find filters the cards;
 * a queued build shows BOSS's hourglass and every row face is 24px; Apps lists its attempts as
 * compact rows; the benchmark and direct-change sheets speak the Factory's words.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { parseFloor } from "../lib/factory-floor";
import { ROWS, attentionGates, buildRow, floor, stations } from "./_helpers/factory-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

const direct = vi.fn(async (_form: FormData) => ({ ok: true, runId: "crun_new" }));
vi.mock("../app/factory/floor-actions", () => ({
  waiveChecksAction: vi.fn(), retryBuildAction: vi.fn(),
  startDirectChangeAction: (form: FormData) => direct(form),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(""),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { FloorView } = await import("../app/factory/floor");
const { BenchmarkSheetBody } = await import("../app/factory/benchmark-sheet");
const { AppsTab, parseApps } = await import("../app/factory/apps-tab");

type W = Record<string, unknown>;
const noop = () => {};
const view = (body: W = floor(), query = "") => (
  <FloorView projectId="p1" actor="ceo" floor={parseFloor(body)} error={null} loading={false} refresh={noop}
             onOpen={noop} onPlan={noop} onDirect={noop} onApps={noop} band="wide" query={query} />
);
const press = async (k: string) => {
  const { act } = await import("react");
  await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); });
  await settle();
};

beforeEach(() => { window.history.replaceState(null, "", "/?area=code_tasks"); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const workflowGate = (): W => ({
  id: "dec_wfgate", kind: "approval", decision_kind: "workflow_gate", decision_id: "dec_wfgate",
  title: "“Approve the PRD” is waiting for you on Ship it", detail: "", role_key: "pm",
  run: { id: "wfr_182", subject: "Ship it", step_id: "prd", step_label: "Approve the PRD", step_index: 2, step_count: 6 },
  actions: [{ action: "approve", label: "Approve", tone: "approve", effect: "The run continues.", needs: [] }],
  answers: [], decide_inline: true, content_hash: "h", at: "2026-10-04T09:00:00.000Z",
  build: { run_id: "crun_inv2", app: "Invoice export", where: "Plan", failing: [], retryable: false },
});

describe("the Floor reads at a glance", () => {
  it("D2: a gate on the build's own workflow run is a card, counted with the decisions", async () => {
    await mount(view(floor({ attention: [workflowGate(), attentionGates()] })));
    expect($('[data-att-id="dec_wfgate"]')).not.toBeNull();
    expect($("[data-testid=fx-needs-h-count]")?.textContent).toBe("1");
    expect($("[data-testid=fx-needs-h-extra]")?.textContent).toBe("· 1 stopped");
  });

  it("D17: Find a build filters the cards too", async () => {
    await mount(view(floor({ attention: [workflowGate(), attentionGates()] }), "team inbox"));
    expect($('[data-att-id="dec_wfgate"]')).toBeNull();
    expect($('[data-att-id="build:crun_cf:gates"]')).not.toBeNull();
  });

  it("D17: J and K move through cards and rows with nothing focused first", async () => {
    await mount(view());
    (document.activeElement as HTMLElement | null)?.blur?.();
    await press("j");
    expect(document.activeElement?.matches("[data-att-card]")).toBe(true);
    await press("j");
    const second = document.activeElement;
    await press("k");
    expect(document.activeElement).not.toBe(second);
  });

  it("D14: BOSS's own build is on the Floor, its app in the name, tagged BOSS", async () => {
    const own = buildRow({ run_id: "crun_cap", app: "e2e-app", title: "QUALITY IMPROVE PASS", internal: true });
    await mount(view(floor({ builds: [own] })));
    expect($("[data-testid=fx-build-open-crun_cap]")?.textContent).toBe("e2e-app");
    expect($("[data-testid=fx-internal-crun_cap]")?.textContent).toBe("BOSS");
  });

  it("D22: a queued build shows BOSS's hourglass; every row face is 24px", async () => {
    await mount(view(floor({ builds: [ROWS.queued, ROWS.implementing] })));
    const queued = $("[data-testid=fx-build-row-crun_q]")!;
    expect(queued.querySelector('[data-face="waiting"]')).not.toBeNull();
    const sizes = $$("[data-testid=fx-build-row-crun_inv2] .ag-av").map((el) => el.getAttribute("data-size"));
    expect(sizes.length).toBeGreaterThan(0);
    expect(sizes.every((s) => s === null || s === "24")).toBe(true);
  });

  it("D21: Apps parses an app's attempts as rows with their stations", async () => {
    const attempt = { ...buildRow({ run_id: "crun_a2", attempt: 2 }), at: "2026-10-04T09:00:00.000Z" };
    const apps = parseApps({ apps: [{ key: "crun_a2", app: "Invoice export", run_id: "crun_a2", builders: ["qa"],
      state: "building", word: "Coding", preview: null, deploys: [{ env: "dev", ok: true, word: "dev ✓" }],
      builds: 2, run_ids: ["crun_a2", "crun_a1"],
      attempts: [attempt, { run_id: "crun_a1", attempt: 1, state: "failed", word: "Failed · Build", at: "2026-10-04T08:00:00.000Z" }] }] });
    expect(apps[0].builds[0].row?.stations.length).toBeGreaterThan(0);
    expect(apps[0].builds[1].row).toBeUndefined();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ apps: [{ key: "crun_a2", app: "Invoice export", run_id: "crun_a2",
      builders: ["qa"], state: "building", word: "Coding", preview: null, deploys: [{ env: "dev", ok: true, word: "dev ✓" }],
      attempts: [attempt] }] })));
    await mount(<AppsTab projectId="p1" runs={[]} previews={[]} query="" onOpen={noop} onNew={noop} />);
    await settle();
    expect($(".fx-app-deploys")?.textContent).toContain("dev ✓");
    // One build: the chevron is there (names align) but has nothing to open.
    expect(($(".fx-app-chev") as HTMLButtonElement | null)?.disabled).toBe(true);
  });

  it("D12: the benchmark sheet speaks the Factory's words", async () => {
    const bench = { id: "bugbox", name: "BugBox", tagline: "Issue tracker", summary: "A small issue tracker with auth.",
      focus: ["auth", "crud"], acceptance_scenarios: [], completion_contract: [], verification_commands: [],
      scoring_dimensions: [], score_baselines: {} };
    await mount(<BenchmarkSheetBody benchmarks={[bench]} builtIds={new Set()} projectId="p1" actor="ceo"
                                    launched={{}} onLaunched={noop} onOpen={noop} />);
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/project|governed|pipeline|code task/i);
    expect($("[data-testid=fx-bench-build-bugbox]")?.textContent).toBe("Build");
    expect(document.querySelector("p")).toBeNull();
  });
});

describe("D13: Direct change passes the panel size and test commands", () => {
  it("posts review_panel_size and test_commands", async () => {
    const { CodeTasksHome } = await import("../app/code-tasks-ui");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(floor())));
    await mount(<CodeTasksHome projectId="p1" codeTasks={[]} apiReachable actor="ceo" />);
    await click($("[data-testid=fx-new-build] + button, [data-testid=fx-new-more]") ?? $(".fx-split-more"));
    await click($('[role="menuitem"][data-testid="fx-new-direct"]') ?? $$('[role="menuitem"]').find((m) => m.textContent === "Direct change") ?? null);
    const set = (sel: string, v: string) => {
      const el = $(sel) as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
      const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
        : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, v);
      el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
    };
    const { act } = await import("react");
    await act(async () => {
      set("[data-testid=fx-direct-title]", "Fix it");
      set("[data-testid=fx-direct-what]", "Do the thing");
      set("[data-testid=fx-direct-tests]", "npm test");
      set("[data-testid=fx-direct-panel]", "2");
    });
    await click($("[data-testid=fx-direct-start]"));
    const form = direct.mock.calls[0]?.[0] as FormData;
    expect(form.get("review_panel_size")).toBe("2");
    expect(form.get("test_commands")).toBe("npm test");
  });
});

void stations;

describe("a build row's tooltip", () => {
  it("cuts a long title to a tooltip's length", async () => {
    const { shortTitle } = await import("../app/factory/build-row");
    const long = "Captain: QUALITY IMPROVE PASS. Measured scorecard gaps: ui=not_scored, ux=not_scored. Do not invent";
    expect(shortTitle(long).split(/\s+/).length).toBeLessThanOrEqual(8);
    expect(shortTitle("Add CSV export")).toBe("Add CSV export");
  });
});
