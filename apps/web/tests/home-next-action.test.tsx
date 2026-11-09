// @vitest-environment happy-dom
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseCommandCenterView, viewerZone } from "../lib/command-center-view";
import { parseCcApps } from "../lib/command-center-apps";
import { nextAction } from "../app/command-center/next-action";
import { apps, failed, freshView, gate, mission, quietView } from "./_helpers/command-center-view-fixture";
import { $, click, mount, rerender, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
const polled = vi.hoisted(() => ({ data: null as unknown, error: null as string | null, refresh: vi.fn() }));
vi.mock("../lib/command-center-view", async (original) => ({ ...await original<typeof import("../lib/command-center-view")>(),
  useCommandCenterView: () => polled }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { CommandCenter } = await import("../app/command-center-ui");
const { AskBossHero } = await import("../app/ask-boss-hero");
const V = parseCommandCenterView;
const gap = { id: "ready:models", kind: "readiness", title: "Connect a model", detail: "No working model is connected.",
  fix: { area: "setup", label: "Connect a model", focus: "models" }, role_key: null };
const action = (wire = quietView(), appRows = parseCcApps({ apps: [] })) => {
  const v = V(wire);
  return nextAction(v, v.attention, appRows, true);
};
const tid = (id: string) => $(`[data-testid="${id}"]`);

beforeEach(() => {
  polled.data = null; polled.error = null; polled.refresh.mockClear();
  window.history.replaceState(null, "", "/?org=org_1&area=overview");
  window.sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 304, headers: new Headers() }) as Response));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const page = (wire = freshView()) => <CommandCenter projectId="p1" orgId="org_1" actor="ceo" tz={viewerZone()}
  initial={V(wire)} initialApps={{ wire: { apps: [] }, etag: "a" }}
  composer={<AskBossHero projectId="p1" orgId="org_1" actor="ceo" layout="hero" deploymentAvailable={false} />} />;

describe("canonical records select the next useful destination", () => {
  it("a prerequisite leads before an empty composer", () => {
    expect(action(freshView({ attention: [gap] }))).toMatchObject({ kind: "setup", title: "Connect a model",
      patch: { area: "setup", focus: "models" } });
  });
  it("the canonical setup count still opens Setup if a detail row is unavailable", () => {
    expect(action(freshView({ status: { key: "setup", tip: "Finish connections" } }))).toMatchObject({ kind: "setup" });
  });
  it("a consequential decision remains first when prerequisites also need work", () => {
    expect(action(freshView({ attention: [gap, gate({ decision_id: "decision-actual" })] }))).toMatchObject({
      kind: "decision", patch: { area: "decisions", decision: "decision-actual", decision_filter: "needs_action" } });
  });
  it("a budget refusal leads with the actual budget reason", () => {
    expect(action(freshView({ attention: [gate()], budget_refusal: { title: "Budget stopped work", detail: "Cap reached" } })))
      .toMatchObject({ kind: "blocked", title: "Budget stopped work", patch: { area: "budget" } });
  });
  it("a failed step opens its run without retrying it", () => {
    expect(action(quietView({ attention: [failed()] }))).toMatchObject({ kind: "stopped", patch: { area: "delivery_workflows" } });
  });
  it.each(["running", "waiting_person", "waiting"])("%s is current work with its real run destination", (state) => {
    expect(action(quietView({ recent: [], missions: [mission({ run_id: "actual-run", state })] })))
      .toMatchObject({ kind: "active", patch: { wfr: "actual-run" } });
  });
  it("a stalled run leads with review, not an inferred running or safe retry claim", () => {
    expect(action(quietView({ missions: [mission({ state: "stalled" })] }))).toMatchObject({ kind: "stopped", label: "Review run" });
  });
  it("a finished build opens the selected delivered attempt", () => {
    const rows = parseCcApps(apps()).filter((a) => a.state === "pr_open");
    expect(action(quietView({ outcomes: [], recent: [] }), rows)).toMatchObject({ kind: "result", title: rows[0].app,
      patch: { area: "code_tasks", build: rows[0].runId } });
  });
  it("an older PR cannot certify the new attempt as a result", () => {
    const row = parseCcApps(apps()).find((a) => a.state === "pr_open")!;
    row.latestState = "building"; row.openRunId = "actual-new-attempt";
    expect(action(quietView({ outcomes: [], recent: [] }), [row])).toMatchObject({ kind: "active",
      patch: { build: "actual-new-attempt" } });
  });
  it("a previous failed attempt does not hide the current build", () => {
    const row = parseCcApps(apps()).find((a) => a.state === "failed")!;
    row.latestState = "building"; row.openRunId = "current-build";
    expect(action(quietView({ outcomes: [], recent: [] }), [row])).toMatchObject({ kind: "active", patch: { build: "current-build" } });
  });
  it.each(["blocked", "budget_wait", "quiet"])("a current %s build does not reuse an older PR label", (state) => {
    const row = parseCcApps(apps()).find((a) => a.state === "pr_open")!;
    row.latestState = state; row.openRunId = "held-current-build";
    expect(action(quietView({ outcomes: [], recent: [] }), [row])).toMatchObject({ kind: "stopped", patch: { build: "held-current-build" } });
    expect(action(quietView({ outcomes: [], recent: [] }), [row]).detail).not.toContain("PR open");
  });
  it("a decision count without its detail row still leads to Approvals", () => {
    const w = freshView(); (w.counts as Record<string, number>).needs_you = 1;
    expect(action(w)).toMatchObject({ kind: "decision", patch: { area: "decisions" } });
  });
  it("a stopped count without row details still outranks older completed work", () => {
    const w = quietView({ recent: [{ run_id: "older", name: "Old result", outcome: "succeeded" }] });
    (w.counts as Record<string, number>).stopped = 1;
    expect(action(w)).toMatchObject({ kind: "stopped", label: "Review work" });
  });
  it("a successful workflow opens its actual finished run", () => {
    expect(action(quietView({ outcomes: [], recent: [{ run_id: "finished-real", name: "Check docs", outcome: "succeeded" }] })))
      .toMatchObject({ kind: "result", patch: { wfr: "finished-real" } });
  });
  it("unreachable stale records cannot present a new current action", () => {
    const v = V(quietView({ attention: [gap] }));
    expect(nextAction(v, v.attention, parseCcApps(apps()), false)).toEqual({ kind: "unknown",
      title: "Current work is unavailable", detail: "Refresh to check setup, decisions and results.", label: "Refresh" });
  });
  it("a partial successful response cannot imply ready from parser defaults", () => {
    const v = V({});
    expect(nextAction(v, [], [], true)).toMatchObject({ kind: "unknown", label: "Refresh" });
  });
  it("project and timestamp alone cannot turn a partial response into empty work", () => {
    const v = V({ project_id: "p1", generated_at: "2026-10-10T18:40:00Z" });
    expect(nextAction(v, [], [], true)).toMatchObject({ kind: "unknown", label: "Refresh" });
  });
  it("missing canonical status cannot use the parser's quiet default even with connection rows", () => {
    const w = freshView(); delete w.status;
    expect(action(w)).toMatchObject({ kind: "unknown", label: "Refresh" });
  });
  it.each(["models", "repo"])("a missing %s row cannot infer empty readiness", (key) => {
    const w = freshView();
    w.systems = (w.systems as { key: string }[]).filter((row) => row.key !== key);
    expect(action(w)).toMatchObject({ kind: "unknown", label: "Refresh" });
  });
  it("an empty connection state is unknown evidence", () => {
    const w = freshView();
    w.systems = [{ key: "models", state: "" }, { key: "repo", state: "OK" }];
    expect(action(w)).toMatchObject({ kind: "unknown", label: "Refresh" });
  });
  it("unknown connection evidence cannot show ready even when the status says first build", () => {
    const w = freshView();
    w.systems = [{ key: "models", state: "Unknown", tone: "grey", word: "Models" }];
    expect(action(w)).toMatchObject({ kind: "unknown", label: "Refresh" });
  });
  it("missing setup evidence does not hide an actual current run", () => {
    expect(action(quietView({ systems: [], missions: [mission({ run_id: "known-current", state: "running" })] })))
      .toMatchObject({ kind: "active", patch: { wfr: "known-current" } });
  });
  it("missing setup evidence does not hide an actual selected result", () => {
    const rows = parseCcApps(apps()).filter((a) => a.state === "pr_open");
    expect(action(quietView({ systems: [], outcomes: [], recent: [] }), rows))
      .toMatchObject({ kind: "result", patch: { build: rows[0].runId } });
  });
  it("a failed build read cannot imply empty work", () => {
    const v = V(freshView());
    expect(nextAction(v, [], null, true, "API unavailable")).toMatchObject({ kind: "unknown", title: "Build results are unavailable" });
  });
  it("canonical empty readiness leaves starting to the existing composer", () => {
    expect(action(freshView())).toEqual({ kind: "empty", title: "Describe your first task",
      detail: "Choose one useful change and the checks it should pass." });
  });
});

describe("the mounted home keeps the main action and draft accessible", () => {
  it("setup link is first and retains the organization and canonical focus", async () => {
    await mount(page(freshView({ attention: [gap] })));
    const link = tid("cc-next-go")!;
    expect(link.getAttribute("href")).toBe("/?org=org_1&area=setup&focus=models");
    expect(tid("cc-compose-disclosure")?.hasAttribute("open")).toBe(false);
    expect(tid("cc-next-action")!.compareDocumentPosition(tid("cc-team-details")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
  it("empty ready state exposes Build directly with no additional start button", async () => {
    await mount(page());
    expect(tid("cc-compose-disclosure")?.hasAttribute("open")).toBe(true);
    expect(tid("split-build")).not.toBeNull();
    expect(tid("cc-next-go")).toBeNull();
    expect(tid("cc-first-start")).toBeNull();
  });
  it("configured roles are secondary and current work counts remain readable", async () => {
    await mount(page());
    expect(tid("cc-team-details")?.hasAttribute("open")).toBe(false);
    expect(tid("cc-team-details")?.querySelector("summary")?.textContent).toBe("7 configured roles, 0 observed working");
  });
  it("typing survives a real polled state change and closing/reopening the same composer", async () => {
    await mount(page());
    const box = tid("askboss-input") as HTMLTextAreaElement;
    await act(async () => { const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(box, "Fix the quoted example, keeping the public API unchanged.");
      box.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(tid("split-build")?.hasAttribute("disabled")).toBe(false);
    polled.data = V(freshView({ attention: [gap] }));
    await rerender(page());
    expect(tid("cc-next-action")?.getAttribute("data-kind")).toBe("setup");
    const disclosure = tid("cc-compose-disclosure") as HTMLDetailsElement;
    disclosure.open = false; disclosure.open = true;
    expect(tid("askboss-input")).toBe(box);
    expect(box.value).toBe("Fix the quoted example, keeping the public API unchanged.");
  });
  it("an unavailable current read offers Refresh and keeps old records secondary", async () => {
    polled.error = "API unavailable";
    await mount(page(freshView({ attention: [gap] })));
    expect(tid("cc-next-go")?.tagName).toBe("BUTTON");
    expect(tid("cc-next-action")?.getAttribute("data-kind")).toBe("unknown");
    await click(tid("cc-next-go"));
    expect(polled.refresh).toHaveBeenCalledTimes(1);
    expect($('.cc-needs fieldset')?.hasAttribute("disabled")).toBe(true);
  });
});
