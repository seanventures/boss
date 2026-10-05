// @vitest-environment happy-dom
/**
 * Outputs name their maker (factory spec §4.5.5): each row's face is the role of the act that
 * made it, from the fixture; machinery is the BOSS glyph; `Ready to merge` only when the server
 * says all four conditions hold; Tests says `k of n commands passed`, never a test count.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { parseCockpit, type Cockpit } from "../lib/cockpit";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";
import { posts, stubFetch } from "./_helpers/cockpit-render";
import { cockpit, cockpitChecksFailed, cockpitPrOpen } from "./_helpers/factory-fixtures";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Outputs, defaultOpen } = await import("../app/factory/outputs");

let toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
beforeEach(() => { toasts = []; window.addEventListener("boss:toast", onToast); });
afterEach(() => { window.removeEventListener("boss:toast", onToast); unmount(); vi.unstubAllGlobals(); });

function view(c: Cockpit, open: Parameters<typeof Outputs>[0]["open"] = null, onRefresh = () => {}) {
  return <Outputs projectId="p1" actor="ceo" cockpit={c} open={open} onOpen={() => {}} onLogs={() => {}} onRefresh={onRefresh} />;
}
const roleIn = (sel: string) => $(`${sel} [data-role]`)?.dataset.role ?? null;

describe("Outputs", () => {
  it("are the Brief, then eight outputs in order, each with the server's word", async () => {
    stubFetch(cockpit());
    await mount(view(parseCockpit(cockpit())));
    // The Brief (what it was asked and how it was built) heads the rail: the old page's
    // contract and meta rows, kept (a-build-page-keeps-what-the-old-page-said.test.tsx).
    expect($$(".fx-out .fx-out-label").map((l) => l.textContent)).toEqual(
      ["Brief", "Pull request", "Checks", "Tests", "Changes", "Comments", "Preview", "Deploy", "Cost"]);
    expect($('[data-testid="fx-out-value-pr"]')?.textContent).toBe("Not yet");
    expect($('[data-testid="fx-out-value-checks"]')?.textContent).toBe("After review");
    expect($('[data-testid="fx-out-value-tests"]')?.textContent).toBe("2 of 2 commands passed");
    expect($('[data-testid="fx-out-value-deploy"]')?.textContent).toBe("Not set up");
    expect($('[data-testid="fx-out-value-cost"]')?.title).toBe("Metered model spend so far");
  });

  it("name the agent whose act made them", async () => {
    stubFetch(cockpit());
    const c = parseCockpit(cockpitPrOpen());
    await mount(view(c));
    expect(roleIn('[data-testid="fx-out-pr"] .fx-out-head')).toBe("software_engineer");
    expect(roleIn('[data-testid="fx-out-tests"] .fx-out-head')).toBe("qa");
    expect(roleIn('[data-testid="fx-out-changes"] .fx-out-head')).toBe("software_engineer");
    expect($('[data-testid="fx-out-preview"] .fx-out-head [data-face="boss"]')).not.toBeNull();
  });

  it("Changes groups tests and code under their makers, files only", async () => {
    stubFetch(cockpit());
    await mount(view(parseCockpit(cockpit()), "changes"));
    expect($('[data-group="Tests"] [data-role]')?.dataset.role).toBe("qa");
    expect($$('[data-testid="fx-out-body-changes"] .fx-file').map((f) => f.textContent)).toEqual(
      ["tests/export.test.ts · 812 bytes", "tests/csv.test.ts · 400 bytes"]);
    expect($('[data-testid="fx-out-head-changes"]')?.title).toBe("Files only. Open the PR for the diff.");
  });

  it("Tests list commands with exit codes, never a test count", async () => {
    stubFetch(cockpit());
    await mount(view(parseCockpit(cockpit()), "tests"));
    const body = $('[data-testid="fx-out-body-tests"]')!.textContent ?? "";
    expect(body).toContain("npm test · exit 0 · ok");
    expect(body).not.toMatch(/\d+ tests/);
    await click($('[data-testid="fx-tests-output"]'));
    expect($('[data-testid="fx-out-body-tests"] pre')?.textContent).toBe("ok\nok");
  });

  it("Ready to merge only when the server says so", async () => {
    stubFetch(cockpit());
    await mount(view(parseCockpit(cockpitPrOpen())));
    expect($('[data-testid="fx-merge-ready"]')?.textContent).toBe("Ready to merge");
    unmount();
    await mount(view(parseCockpit(cockpitChecksFailed())));
    expect($('[data-testid="fx-merge-ready"]')?.textContent).toBe("Not ready · 1 open blocker");
    expect($('[data-testid="fx-merge-ready"]')?.hasAttribute("data-ready")).toBe(false);
  });

  it("failed checks open by default; a PR waiting on you opens the PR", () => {
    expect(defaultOpen(parseCockpit(cockpitChecksFailed()))).toBe("checks");
    expect(defaultOpen(parseCockpit(cockpitPrOpen()))).toBe("pr");
    expect(defaultOpen(parseCockpit(cockpit()))).toBeNull();
  });

  it("Comments group by severity with the author's face, and Resolve posts", async () => {
    stubFetch(cockpit());
    const refreshed = vi.fn();
    await mount(view(parseCockpit(cockpitChecksFailed()), "comments", refreshed));
    expect($('[data-severity="blocker"] .fx-out-sub')?.textContent).toBe("Blocker");
    expect($('[data-testid="fx-comment-rc1"] [data-role]')?.dataset.role).toBe("adversarial_reviewer");
    expect($('[data-testid="fx-comment-rc1"]')?.textContent).toContain("src/invoices/export.ts:42");
    await click($('[data-testid="fx-resolve-rc1"]'));
    expect(posts[0]).toEqual({ url: "/v1/code-tasks/crun_cf/review-comments/rc1/resolve",
                               body: { actor: "ceo", status: "addressed", note: "" } });
    expect(toasts).toContain("Comment resolved");
    expect(refreshed).toHaveBeenCalled();
  });

  it("Cost per agent: face, turns, size, metered dollars", async () => {
    stubFetch(cockpit());
    await mount(view(parseCockpit(cockpit()), "cost"));
    const rows = $$('[data-testid="fx-out-body-cost"] .fx-cost-row');
    expect(rows.map((r) => r.dataset.role)).toEqual(["software_engineer", "qa", "chief_architect"]);
    expect(rows[0].textContent).toContain("41 turns");
    expect(rows[0].textContent).toContain("$0.22");
  });

  it("subscription-only spend says Included in plan with its tooltip, and no dollars", async () => {
    stubFetch(cockpit());
    const c = parseCockpit(cockpit({ metered: false, cost_usd: 0, outputs: { ...(cockpit().outputs as Record<string, unknown>),
      cost: { word: "Included in plan", metered_usd: 0, subscription_calls: 12, by_agent: [{ role_key: "qa", turns: 3, tokens: 1000, usd: 0 }], by_station: {} } } }));
    await mount(view(c, "cost"));
    expect($('[data-testid="fx-out-value-cost"]')?.textContent).toBe("Included in plan");
    expect($('[data-testid="fx-out-value-cost"]')?.title).toBe("Ran on a subscription. No metered spend.");
    expect($('[data-testid="fx-out-body-cost"]')?.textContent).not.toContain("$");
  });

  it("a failed preview offers no Open link; a running one opens through the console's origin", async () => {
    stubFetch(cockpit());
    const base = cockpitPrOpen();
    const failed = parseCockpit({ ...base, outputs: { ...(base.outputs as Record<string, unknown>),
      preview: { id: "prev_1", status: "failed", word: "Failed", error: "port in use" } } });
    await mount(view(failed, "preview"));
    expect($('[data-testid="fx-out-value-preview"]')?.textContent).toBe("Failed");
    expect($('[data-testid="fx-preview-open"]')).toBeNull();
    expect($('[data-testid="fx-preview-restart"]')).not.toBeNull();
    unmount();
    await mount(view(parseCockpit(cockpitPrOpen()), "preview"));
    expect($('[data-testid="fx-preview-open"]')?.getAttribute("href")).toBe("/v1/previews/prev_1/web/");
  });
});

describe("the Logs drawer", () => {
  it("streams the preview's startup log through the console's own origin", async () => {
    const opened: string[] = [];
    class FakeSource {
      static CLOSED = 2;
      readyState = 0;
      onerror: (() => void) | null = null;
      constructor(url: string) { opened.push(url); }
      addEventListener() {}
      close() {}
    }
    vi.stubGlobal("EventSource", FakeSource as unknown as typeof EventSource);
    stubFetch(cockpit());
    const { LogsDrawer } = await import("../app/factory/logs-drawer");
    await mount(<LogsDrawer projectId="p1" runId="crun_pr" previewId="prev_1" deployId={null} tab="preview" onTab={() => {}} onClose={() => {}} />);
    expect(opened).toEqual(["/api/preview-logs?preview_id=prev_1"]);
    expect($('[data-testid="preview-log"]')?.dataset.state).toBe("connecting");
  });

  it("the Build tab reads this run's activity, oldest first", async () => {
    stubFetch(cockpit());
    const { LogsDrawer } = await import("../app/factory/logs-drawer");
    await mount(<LogsDrawer projectId="p1" runId="crun_pr" previewId={null} deployId={null} tab="build" onTab={() => {}} onClose={() => {}} />);
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.startsWith("/api/activity?") && u.includes("run_id=crun_pr") && u.includes("order=asc"))).toBe(true);
    expect($('[data-testid="fx-log-build"]')?.textContent).toContain("Cloned the repo");
  });
});
