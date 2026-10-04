// @vitest-environment happy-dom
/**
 * A build's cockpit in every state (factory spec §4.7, cockpit columns), rendered from the
 * Phase 0 fixtures through the real poll and parsers, asserted by exact copy. Plus the header's
 * actions (Stop → confirm, Retry → Attempt n, Steer → queued), the attempt menu, and the Esc chain.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";
import { posts, stubFetch } from "./_helpers/cockpit-render";
import { cockpit, cockpitCancelled, cockpitChecksFailed, cockpitFailed, cockpitPrOpen, stations } from "./_helpers/factory-fixtures";

const retry = vi.fn(async () => ({ runId: "crun_inv3", attempt: 3 }));
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  retryCodeTaskWithStateAction: () => retry(),
  startPreviewAction: async () => ({ url: "/v1/previews/p/web/" }),
}));
vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span data-testid="live-deploy" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Cockpit } = await import("../app/factory/cockpit");

let toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
const onClose = vi.fn();
const onOpenBuild = vi.fn();
const view = (runId = "crun_inv2") => <Cockpit projectId="p1" runId={runId} actor="ceo" onClose={onClose} onOpenBuild={onOpenBuild} />;
const text = (sel: string) => $(sel)?.textContent ?? "";
const key = async (k: string) => {
  const { act } = await import("react");
  await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); });
  await settle();
};

beforeEach(() => {
  toasts = [];
  onClose.mockReset();
  onOpenBuild.mockReset();
  retry.mockClear();
  window.addEventListener("boss:toast", onToast);
});
afterEach(() => { window.removeEventListener("boss:toast", onToast); unmount(); vi.unstubAllGlobals(); });

describe("the cockpit header says one state", () => {
  it("building: word, k of n, elapsed, metered cost, Steer and Stop", async () => {
    stubFetch(cockpit());
    await mount(view());
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("Coding");
    expect(text('[data-testid="fx-k"]')).toBe("2 of 8");
    expect($('[data-testid="fx-k"]')?.title).toBe("Stations done. BOSS doesn't guess finish times.");
    expect(text('[data-testid="fx-elapsed"]')).not.toBe("");
    expect(text('[data-testid="fx-cost"]')).toBe("$0.31");
    expect($('[data-testid="fx-steer"]')?.title).toBe("Guide the agents. They read it between stations.");
    expect($('[data-testid="fx-stop"]')?.title).toBe("Keeps the work so far. Retry resumes it.");
    expect($('[data-testid="fx-strip"]')).toBeNull();
    expect(text("h1")).toBe("Invoice export");
    expect(document.activeElement?.tagName).toBe("H1");
    expect(text('[data-testid="fx-origin"]')).toBe("from Ship it #182 ↗");
    expect(document.body.textContent).not.toMatch(/%|ETA|estimated|almost/);
  });

  it("an unmetered build shows no cost", async () => {
    stubFetch(cockpit({ metered: false, cost_usd: 0 }));
    await mount(view());
    expect($('[data-testid="fx-cost"]')).toBeNull();
  });

  it("queued: all stations pending, outputs say Not yet", async () => {
    stubFetch(cockpit({ state: { state: "queued", word: "Queued", tip: "Waiting for a worker", verb: "Waiting", station: null, k: 0, n: 8, rounds: 0, preview: null },
                        stations: stations(null), story: [], now: null,
                        outputs: { merge_ready: { ready: false, word: "Building" }, comments: { word: "None", items: [] },
                                   deploy: { word: "Not set up", items: [] }, cost: { word: "—" } } }));
    await mount(view());
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("Queued");
    expect(text('[data-testid="fx-k"]')).toBe("0 of 8");
    expect($$(".fx-bay").every((b) => b.dataset.status === "pending")).toBe(true);
    expect(text('[data-testid="fx-out-value-pr"]')).toBe("Not yet");
    expect(text('[data-testid="fx-out-value-tests"]')).toBe("Not run");
  });

  it("checks failed: the strip with Retry and Waive…, Review stuck, Checks open with labels and reviewer faces", async () => {
    stubFetch(cockpitChecksFailed());
    await mount(view("crun_cf"));
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("Checks failed");
    expect($('[data-testid="fx-strip"] [data-testid="ag-att-build:crun_cf:gates"]')).not.toBeNull();
    expect($('.fx-bay[data-key="review"]')?.dataset.status).toBe("stuck");
    expect($('[data-testid="fx-out-body-checks"]')).not.toBeNull();
    expect(text('[data-testid="fx-gate-code_review_passed"]')).toContain("Review passed");
    expect($('[data-testid="fx-gate-code_review_passed"] [data-role]')?.dataset.role).toBe("adversarial_reviewer");
    expect($('[data-testid="fx-gate-security_clean"] [data-role]')?.dataset.role).toBe("security_reviewer");
    expect(text('[data-testid="fx-merge-ready"]')).toBe("Not ready · 1 open blocker");
  });

  it("PR open: the PR row is open, merge readiness says Ready to merge, preview flag", async () => {
    stubFetch(cockpitPrOpen());
    await mount(view("crun_pr"));
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("PR open");
    expect($('[data-testid="fx-out-body-pr"]')).not.toBeNull();
    expect(text('[data-testid="fx-pr-open"]')).toBe("Open PR ↗");
    expect(text('[data-testid="fx-merge-ready"]')).toBe("Ready to merge");
    expect($('[data-testid="fx-merge-ready"]')?.title).toBe("From checks, tests and review comments");
    expect(text('[data-testid="fx-preview-flag"]')).toBe("Preview live ↗");
    expect($('[data-testid="fx-stop"]')).toBeNull();
    expect(text('.fx-bay[data-key="pr"] .fx-bay-link')).toBe("#842 ↗");
  });

  it("failed: Failed · Build, the Build bay stuck, the Story's last act carries the failure line", async () => {
    stubFetch(cockpitFailed());
    await mount(view());
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("Failed · Build");
    expect($('.fx-bay[data-key="build"]')?.dataset.status).toBe("stuck");
    expect(text('[data-testid="fx-story-failure"]')).toContain("Failed · retry may fix");
    expect($('[data-testid="fx-story-failure"] [title]')?.getAttribute("title")).toBe("npm test exited 1");
  });

  it("cancelled: Stopped by you, the rest skipped, a You · Stopped row", async () => {
    stubFetch(cockpitCancelled());
    await mount(view());
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("Stopped by you");
    for (const k of ["check", "review", "pr", "preview", "ship"]) expect($(`.fx-bay[data-key="${k}"]`)?.dataset.status).toBe("skipped");
    expect(text('[data-testid="fx-story-y2"] .fx-verb')).toBe("Stopped");
  });

  it("budget wait and quiet hold still; quiet opens Logs once", async () => {
    stubFetch(cockpit({ state: { state: "quiet", word: "Quiet · 24m", tip: "No progress for 24m. It may be stuck.", verb: "Coding", station: "build", k: 2, n: 8, rounds: 0, preview: null } }));
    await mount(view());
    expect(text('[data-testid="fx-state"] .fx-state-word')).toBe("Quiet · 24m");
    expect($('[data-testid="fx-cockpit"]')?.dataset.still).toBe("true");
    expect($$('[data-testid="fx-cockpit"] [data-state="working"]')).toHaveLength(0);
    expect($('[data-testid="fx-logs"]')).not.toBeNull();
  });

  it("loading is a skeleton; an unreachable API says Can't reach BOSS with Retry", async () => {
    stubFetch(() => new Response(JSON.stringify({ detail: "down" }), { status: 503 }));
    await mount(view());
    expect(text('[data-testid="fx-unreachable"]')).toContain("Can't reach BOSS");
    expect($('[data-testid="fx-unreachable"] button')?.title).toBe("down");
  });
});

describe("the header's actions", () => {
  it("Stop asks first, posts cancel, says Build stopped", async () => {
    stubFetch(cockpit());
    await mount(view());
    await click($('[data-testid="fx-stop"]'));
    expect(text('[data-testid="fx-stop-confirm"]')).toContain("Stop this build?");
    expect(posts).toHaveLength(0);
    await click($('[data-testid="fx-stop-yes"]'));
    expect(posts[0].url).toBe("/v1/code-tasks/crun_inv2/cancel");
    expect(posts[0].body).toEqual({ actor: "ceo" });
    expect(toasts).toContain("Build stopped");
  });

  it("Retry from ⋯ starts Attempt 3 and opens it", async () => {
    stubFetch(cockpitFailed());
    await mount(view());
    await click($('[data-testid="fx-more"]'));
    await click($('[data-testid="fx-menu-retry"]'));
    expect(toasts).toContain("Attempt 3 started");
    expect(onOpenBuild).toHaveBeenCalledWith("crun_inv3");
  });

  it("Steer posts guidance and shows it queued in the Story", async () => {
    stubFetch(cockpit({ story: [] }));
    await mount(view());
    await click($('[data-testid="fx-steer"]'));
    const input = $('[data-testid="fx-steer-input"]') as HTMLInputElement;
    expect(input.placeholder).toMatch(/^Tell \S.* …$/);
    expect(input.placeholder).not.toContain("the agents");
    const { act } = await import("react");
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "Use the shared CSV writer");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { ($('[data-testid="fx-steer-box"]') as HTMLFormElement).requestSubmit(); });
    await settle();
    expect(posts[0]).toEqual({ url: "/v1/code-tasks/crun_inv2/steer", body: { actor: "ceo", guidance: "Use the shared CSV writer" } });
    expect(toasts).toContain("Guidance queued");
    expect(text('[data-testid="fx-steer-queued"]')).toBe("Queued for next station");
  });

  it("a steer the agent read says Read by <name>", async () => {
    stubFetch(cockpit());
    await mount(view());
    expect(text('[data-testid="fx-steer-read"]')).toMatch(/^Read by /);
  });

  it("attempt 2 ▾ lists the lineage and switches build", async () => {
    stubFetch(cockpit());
    await mount(view());
    expect(text('[data-testid="fx-attempts"]')).toBe("attempt 2 ▾");
    await click($('[data-testid="fx-attempts"]'));
    const items = $$('[data-testid="fx-attempts-menu"] [role="menuitem"]');
    expect(items.map((i) => i.textContent?.split(" · ").slice(0, 2).join(" · "))).toEqual(["Attempt 2 · Coding", "Attempt 1 · Failed"]);
    await click(items[1]);
    expect(onOpenBuild).toHaveBeenCalledWith("crun_inv1");
  });

  it("Esc closes Logs, then clears the filter, then goes back to the Floor", async () => {
    stubFetch(cockpit());
    await mount(view());
    await key("l");
    expect($('[data-testid="fx-logs"]')).not.toBeNull();
    await click($('[data-testid="fx-bay-build"]'));
    expect(text('[data-testid="fx-story-filter"]')).toBe("Build ×");
    await key("Escape");
    expect($('[data-testid="fx-logs"]')).toBeNull();
    expect($('[data-testid="fx-story-filter"]')).not.toBeNull();
    await key("Escape");
    expect($('[data-testid="fx-story-filter"]')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    await key("Escape");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("a bay filters the Story to its station", async () => {
    stubFetch(cockpit());
    await mount(view());
    await click($('[data-testid="fx-bay-tests"]'));
    const rows = $$('[data-testid="fx-story"] .fx-ck-row');
    expect(rows.map((r) => r.dataset.testid)).toEqual(["fx-story-act_TESTS0000000001"]);
  });

  it("a face opens its Act card", async () => {
    stubFetch(cockpit());
    await mount(view());
    await click($('[data-testid="fx-story-face-act_BUILD0000000001"]'));
    expect($('[data-testid="fx-act-pop"] [data-testid="ag-act-card"]')).not.toBeNull();
  });
});
