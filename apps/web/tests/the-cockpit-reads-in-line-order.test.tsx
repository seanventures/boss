// @vitest-environment happy-dom
/**
 * Walk D5, D8, D10, D11, D16, D20: the Story reads in the Line's order with one head per
 * station; a steer says whether it was read; a spend that rounds to nothing is not shown; the
 * Logs drawer is a sheet on <body> (a phone shows it full-screen) speaking plain sentences; a
 * preview says `Starting…` the moment it is pressed.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit, cockpitPrOpen } from "./_helpers/factory-fixtures";

const startPreview = vi.fn(() => new Promise(() => {}));
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  startPreviewAction: () => startPreview(),
}));
vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span data-testid="live-deploy" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Cockpit } = await import("../app/factory/cockpit");
type W = Record<string, unknown>;

let toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
const view = () => <Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />;
const at = (m: number) => new Date(Date.parse("2026-10-04T09:12:00.000Z") + m * 60_000).toISOString();
const act = (id: string, station: string, role: string | null, title: string, m: number, cost = 0.05): W => ({
  type: "act", id, station, title, seat: null, round: 1, word: "Done", verdict: null, files: null, tool_calls: 0,
  comments_authored: 0, comments_fixed: 0, said: null, failure: null,
  act: { act_id: id, role_key: role, kind: "stage", status: "complete", verb: "Working", label: title,
         started_at: at(m), ended_at: at(m + 1), cost_usd: cost },
});

beforeEach(() => { toasts = []; window.addEventListener("boss:toast", onToast); });
afterEach(() => { window.removeEventListener("boss:toast", onToast); unmount(); vi.unstubAllGlobals(); });

describe("the cockpit reads in the Line's order", () => {
  it("D8: one head per station, in the Line's order — Check once, though tests re-ran after the PR", async () => {
    stubFetch(cockpit({ story: [
      { type: "boss", id: "b1", station: "plan", what: "cloned", text: "Cloned the repo", at: at(0) },
      act("act_TESTS00000000001", "tests", "qa", "Write tests", 1),
      act("act_BUILD00000000001", "build", "software_engineer", "Implement", 2),
      act("act_REVIEW0000000001", "review", "adversarial_reviewer", "Code review", 3),
      act("act_CHECK00000000001", "check", "qa", "Run tests", 4),
      { type: "boss", id: "b2", station: "pr", what: "opened_pr", text: "Opened PR #842", at: at(5) },
      { type: "boss", id: "b3", station: "check", what: "ran_tests", text: "Ran tests", at: at(6) },
    ] }));
    await mount(view());
    const heads = $$(".fx-story-station").map((h) => h.textContent);
    expect(heads).toEqual(["Plan", "Tests", "Build", "Check", "Review", "PR"]);
    const check = $$(".fx-story-group").find((g) => g.dataset.station === "check")!;
    expect(check.querySelectorAll(".fx-ck-row")).toHaveLength(2);
  });

  it("D15: no `round n` on a build that never went round", async () => {
    const a = act("act_CHECK00000000003", "check", "qa", "Run tests", 4);
    a.round = 3;
    stubFetch(cockpit({ story: [a] }));
    await mount(view());
    expect($('[data-testid="fx-story-act_CHECK00000000003"]')?.textContent).not.toContain("round");
  });

  it("D16: a stage whose spend rounds to nothing shows no $0.00", async () => {
    stubFetch(cockpit({ story: [act("act_TESTS00000000001", "tests", "qa", "Write tests", 1, 0.004)] }));
    await mount(view());
    expect($('[data-testid="fx-story-act_TESTS00000000001"]')?.textContent).not.toContain("$0.00");
  });

  it("D11: a steer the build ended before reading says so", async () => {
    stubFetch(cockpit({ story: [
      { type: "you", id: "y1", station: "build", what: "steered", actor: "ceo", text: "Use the shared writer",
        at: at(3), word: "Not read · build ended" },
    ] }));
    await mount(view());
    expect($('[data-testid="fx-steer-unread"]')?.textContent).toBe("Not read · build ended");
    expect($('[data-testid="fx-steer-queued"]')).toBeNull();
  });

  it("D5 + D20: Logs is a sheet on <body>, and the build log speaks sentences", async () => {
    stubFetch(cockpit(), (url) => (url.includes("/api/activity") ? { items: [
      { id: "a1", source: "code_task", kind: "code_task.github_plane", title: "Code Task Github Plane", created_at: at(0) },
      { id: "a2", source: "code_task", kind: "code_task.pr_opened", title: "Code Task Pr Opened", created_at: at(5), data: { number: 842 } },
      { id: "a3", source: "code_task", kind: "code_task.started", title: "Code Task Started", created_at: at(0) },
    ] } : undefined));
    await mount(view());
    await click($('[data-testid="fx-logs-toggle"]'));
    await settle();
    const drawer = $('[data-testid="fx-logs"]')!;
    expect(drawer.parentElement).toBe(document.body);
    const log = $('[data-testid="fx-log-build"]')?.textContent ?? "";
    expect(log).toContain("Opened pull request #842");
    expect(log).toContain("Work started");
    expect(log).not.toMatch(/Code Task|Github Plane/);
  });

  it("D10 + D9: Run preview says Starting… and Preview starting before the boot answers", async () => {
    const body = cockpitPrOpen();
    (body.outputs as W).preview = null;
    (body.state as W).preview = null;
    stubFetch(body);
    await mount(view());
    const head = $('[data-testid="fx-out-head-preview"]');
    if (head?.getAttribute("aria-expanded") !== "true") await click(head);
    await click($('[data-testid="fx-preview-run"]'));
    expect(toasts).toContain("Preview starting");
    expect($('[data-testid="fx-preview-starting"]')?.textContent).toBe("Starting…");
  });
});
