// @vitest-environment happy-dom
/**
 * THE QUALITY BAR, PHASE 2 (BOSSAPP-1362), clicked through:
 *   - Setup › Quality bar: Prototype · Standard · Production with an icon each, the chosen one's
 *     plain words (what blocks, what becomes a follow-up), and what no preset changes; a press
 *     writes the project setting.
 *   - Setup › Advanced › Each gate: the floor reads "Always blocks" with no choice; any other gate
 *     is Preset · Blocks · Follow-up; Reset hands every gate back to the preset.
 *   - New work › Quality: opens on the project's bar, sends the bar picked for this run only when
 *     it differs; a lower bar is held for a person without settings:update.
 *   - The run report lists the follow-ups (what did not block), as the PR does.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { fakeApi, flush, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";
import type { QualityBarView, SetupView } from "../lib/setup-types";
import { ProjectPage } from "../app/projects/project-page";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_SETTINGS, FX_WORK, FX_WORK_PLAN,
} from "../lib/project-experience-fixtures";
import { mountAs, stubFetch, tid, unmount, type Call, type Role } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PRESETS: QualityBarView["presets"] = [
  { key: "prototype", label: "Prototype", line: "Fastest. Blocks only on the basics; everything else is a follow-up.",
    blocks: ["It doesn't build or start"], follow_ups: ["Review findings, even a blocker"] },
  { key: "standard", label: "Standard", line: "The default. Also blocks on a review blocker; agents fix the rest after.",
    blocks: ["A review blocker"], follow_ups: ["Missing or partial must-haves"] },
  { key: "production", label: "Production", line: "Strictest. Blocks on everything, and the second test pass must finish.",
    blocks: ["Review blockers and majors"], follow_ups: ["Minor review findings"] },
];
const ALWAYS = "Below Production nothing stops to ask you mid-build: agents fix and carry on. A release to production always applies Production and waits for a person's approval, whatever the preset.";

function qbView(over: Partial<QualityBarView> = {}): QualityBarView {
  return {
    key: "quality_bar", gates_key: "quality_bar_gates", bar: "standard", default: "standard", source: "default",
    permission: "settings:update", always: ALWAYS, presets: PRESETS, overrides: {},
    gates: [
      { gate: "security_clean", label: "Security clean", floor: true, level: "block", effective: "block",
        presets: { prototype: "block", standard: "block", production: "block" } },
      { gate: "code_review_passed", label: "Review passed", floor: false, level: "default", effective: "block",
        note: "Standard blocks on a BLOCKER; Production on MAJOR too.",
        presets: { prototype: "follow_up", standard: "block", production: "block" } },
    ],
    ...over,
  };
}

const withQb = (qb: QualityBarView): SetupView => ({ ...board(), quality_bar: qb });

// ── Setup ────────────────────────────────────────────────────────────────────────────────────

describe("Setup › Quality bar", () => {
  let mounted: Mounted | null = null;
  afterEach(() => { mounted?.unmount(); mounted = null; });

  it("shows the three presets with icons, the chosen one's words and what never changes", async () => {
    const api = fakeApi(withQb(qbView()));
    mounted = await mountBoard(api.view);
    const row = tid("su-row-quality-bar")!;
    expect(row).toBeTruthy();
    for (const key of ["prototype", "standard", "production"]) {
      const b = tid(`su-qb-${key}`)!;
      expect(b.className).toContain("su-btn");
      expect(b.querySelector("svg")).toBeTruthy();                 // every option carries its icon
    }
    expect(tid("su-qb-standard")!.getAttribute("aria-checked")).toBe("true");
    expect(tid("su-qb-standard")!.className).toContain("su-btn-primary");
    expect(tid("su-qb-line")!.textContent).toContain("Also blocks on a review blocker");
    expect(tid("su-qb-blocks")!.textContent).toContain("A review blocker");
    expect(tid("su-qb-followups")!.textContent).toContain("Missing or partial must-haves");
    expect(tid("su-qb-always")!.textContent).toContain("waits for a person's approval");
  });

  it("a press saves the project's bar", async () => {
    const api = fakeApi(withQb(qbView()));
    mounted = await mountBoard(api.view);
    api.answer = (c) => (c.op === "quality_bar" ? { status: 200, body: { key: "quality_bar", value: "production" } } : null);
    api.view = withQb(qbView({ bar: "production", source: "project" }));
    await act(async () => { tid("su-qb-production")!.click(); });
    await flush();
    const put = api.calls.find((c) => c.op === "quality_bar")!;
    expect(put.method).toBe("POST");
    expect(put.body).toMatchObject({ op: "quality_bar", value: "production" });
    expect(tid("su-qb-production")!.getAttribute("aria-checked")).toBe("true");
  });
});

describe("Setup › Advanced › Each gate", () => {
  let mounted: Mounted | null = null;
  afterEach(() => { mounted?.unmount(); mounted = null; });

  it("the floor always blocks; another gate is Preset, Blocks or Follow-up; Reset clears", async () => {
    const api = fakeApi(withQb(qbView()));
    mounted = await mountBoard(api.view, "?area=setup&focus=quality_gates");
    const floor = tid("su-qg-security_clean")!;
    expect(floor.textContent).toContain("Always blocks");
    expect(floor.querySelector("button")).toBeNull();
    expect(tid("su-qg-code_review_passed-default")!.textContent).toContain("Preset · Blocks");
    expect(tid("su-qg-code_review_passed-default")!.getAttribute("aria-checked")).toBe("true");
    expect(tid("su-qg-code_review_passed-follow_up")!.querySelector("svg")).toBeTruthy();
    api.view = withQb(qbView({ overrides: { code_review_passed: "follow_up" } }));
    await act(async () => { tid("su-qg-code_review_passed-follow_up")!.click(); });
    await flush();
    const put = api.calls.find((c) => c.op === "quality_bar_gates")!;
    expect(put.body).toMatchObject({ value: { code_review_passed: "follow_up" } });
    await act(async () => { tid("su-qg-reset")!.click(); });
    await flush();
    const reset = api.calls.filter((c) => c.op === "quality_bar_gates").at(-1)!;
    expect(reset.body.value).toBeNull();
  });
});

// ── New work ─────────────────────────────────────────────────────────────────────────────────

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(el, value);
    (el as HTMLElement).dispatchEvent(new Event("input", { bubbles: true }));
  });
};

function engine(bar = "standard") {
  return stubFetch((c: Call) => {
    if (c.path === "/v1/tickets/resolve") return { body: { refs: [], tickets: [], errors: [], connected: 0 } };
    if (c.path === "/v1/pipeline-profiles") return { body: { default: "balanced", items: [] } };
    if (c.method === "POST" && c.path === `/v1/projects/${P}/work/understand`) return { body: { status: "empty" } };
    if (c.method === "GET" && c.path === `/v1/projects/${P}/work/plan`) {
      return { body: { ...FX_WORK_PLAN, defaults: { tracker: null, targets: [], target: null, tell: { routes: [] }, priority: "normal",
                                                    quality_bar: { bar, always: ALWAYS, presets: PRESETS } } } };
    }
    if (c.method !== "GET") return { body: { ok: true, run_id: "wfr_new" } };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview: FX_OVERVIEW, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
                                                onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return { body: table[m[2]] };
    }
    return undefined;
  });
}

async function openNewWork(role: Role = "owner") {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=work`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, role);
  await settle(10);
  await press(tid("px-head-new-work"));
  await settle(6);
}
const sentWork = (calls: Call[]) => calls.find((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`)?.body as
  { quality_bar?: string } | undefined;

describe("New work › Quality", () => {
  beforeEach(() => { try { window.localStorage.clear(); } catch { /* none */ } });
  afterEach(() => { unmount(); });

  it("opens on the project's bar and sends nothing when it is kept", async () => {
    const calls = engine("standard");
    await openNewWork();
    expect(tid("px-nw-quality")?.textContent).toContain("Standard");
    expect(tid("px-nw-quality")?.querySelector("svg")).toBeTruthy();
    await type(tid("px-compose-text"), "Add a health route that reports the pool");
    await press(tid("px-compose-start"));
    expect(sentWork(calls)).toBeTruthy();
    expect(sentWork(calls)?.quality_bar).toBeUndefined();
  });

  it("sends the bar picked for this run", async () => {
    const calls = engine("standard");
    await openNewWork();
    await press(tid("px-nw-chip-quality"));
    expect(tid("px-nw-quality-always")?.textContent).toContain("production");
    await press(tid("px-nw-quality-production"));
    expect(tid("px-nw-quality")?.textContent).toContain("Production");
    await type(tid("px-compose-text"), "Add a health route that reports the pool");
    await press(tid("px-compose-start"));
    expect(sentWork(calls)?.quality_bar).toBe("production");
  });

  it("holds a lower bar for a person without settings:update, never a stricter one", async () => {
    engine("standard");
    await openNewWork("operator");
    await press(tid("px-nw-chip-quality"));
    const lower = tid("px-nw-quality-prototype") as HTMLButtonElement;
    expect(lower.disabled).toBe(true);
    expect(lower.textContent).toContain("needs an owner or admin");
    expect((tid("px-nw-quality-production") as HTMLButtonElement).disabled).toBe(false);
  });
});

// ── the run ──────────────────────────────────────────────────────────────────────────────────

describe("the run report lists the follow-ups", () => {
  let root: Root | null = null;
  let host: HTMLElement | null = null;
  afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; host = null; });

  it("shows what did not block, P1 first, with the bar", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      ok: true, workflow: "Minor Feature", status: "completed", elapsed_minutes: 9, working_minutes: 8,
      waiting_minutes: 0, total_usd: 1, list_usd: 1, steps: [], contributors: [], findings: [],
      product: { name: "Lead triage", build: { files_changed: 2, verdicts: {}, followups: {
        bar: "standard", count: 2, p1: 1, words: "2 follow-ups listed for later",
        items: [{ tier: "P1", severity: "major", text: "Must-have 2 (P1) missing: export CSV" },
                { tier: "P2", severity: "minor", text: "rename a helper" }] } } },
      next_steps: [],
    })));
    const { RunReportPanel } = await import("../app/run-report-ui");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => { root!.render(<RunReportPanel runId="wfr_1" projectId={P} expanded />); });
    await settle(6);
    const section = tid("run-report-followups")!;
    expect(section.textContent).toContain("Follow-ups");
    expect(section.textContent).toContain("standard bar");
    expect(section.textContent).toContain("export CSV");
    expect(tid("run-report-followups-words")!.textContent).toContain("2 follow-ups listed for later");
    expect(section.querySelector("li")!.getAttribute("data-priority")).toBe("P1");
  });
});
