// @vitest-environment happy-dom
/**
 * Every Floor state, rendered from its fixture and read back by its exact words (factory spec
 * §4.7): the row's word, `k of n` only where it counts, the active station's status, and the
 * section the build lands in. Plus loading, unreachable, empty, Paused, stopped and shipped.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseFloor } from "../lib/factory-floor";
import { ROWS, emptyFloor, floor } from "./_helpers/factory-fixtures";
import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { FloorView } = await import("../app/factory/floor");

const noop = () => {};
type Over = Partial<React.ComponentProps<typeof FloorView>>;
const view = (body: Record<string, unknown> | null, over: Over = {}) => (
  <FloorView projectId="p1" actor="ceo" floor={body ? parseFloor(body) : null} error={null} loading={false}
             refresh={noop} onOpen={noop} onPlan={noop} onDirect={noop} onApps={noop} band="wide" {...over} />
);
const one = (row: Record<string, unknown>) => floor({ attention: [], builds: [row], outcomes: [], stopped: [] });
const nowText = (id: string) => [...$(`[data-testid=fx-build-row-${id}] .fx-row-now`)!.children]
  .filter((c) => !c.getAttribute("aria-hidden")).map((c) => c.textContent);

afterEach(() => unmount());

describe("a building row says exactly where it is", () => {
  it.each([
    ["queued", "crun_q", ["Queued", "0 of 8"], null],
    ["starting", "crun_s", ["Starting", "0 of 8"], "waiting"],
    ["planning", "crun_p", ["Planning", "0 of 8"], "working"],
    ["needs_plan", "crun_np", ["Needs you · plan", "1 of 8"], "needs_you"],
    ["writing_tests", "crun_wt", ["Testing", "1 of 8"], "working"],
    ["implementing", "crun_inv2", ["Coding", "2 of 8"], "working"],
    ["checking", "crun_ck", ["Testing", "3 of 8"], "working"],
    ["reviewing", "crun_rv", ["Reviewing", "4 of 8"], "working"],
    ["repairing", "crun_rp", ["Coding · round 2"], "working"],
    ["checks_failed", "crun_cf", ["Checks failed"], "stuck"],
    ["budget_wait", "crun_bw", ["Waiting · budget"], "waiting"],
    ["budget_refused", "crun_br", ["Budget refused"], "stuck"],
    ["quiet", "crun_qt", ["Quiet · 24m"], "working"],
    ["blocked", "crun_bl", ["Blocked"], "stuck"],
  ])("%s", async (name, id, words, active) => {
    await mount(view(one(ROWS[name])));
    const row = $(`[data-testid=fx-build-row-${id}]`)!;
    expect(row).not.toBeNull();
    // Elapsed ticks after mount; only the server's words and the count are asserted here.
    expect(nowText(id).slice(0, words.length)).toEqual(words);
    const bay = row.querySelector("[data-active]");
    expect(bay?.getAttribute("data-status") ?? null).toBe(active);
    expect(row.textContent).not.toMatch(/%|ETA|estimated|almost/i);
  });

  it("the review station draws one face per seat", async () => {
    await mount(view(one(ROWS.reviewing)));
    const review = $("[data-testid=fx-build-row-crun_rv] [data-key=review]")!;
    expect([...review.querySelectorAll(".ag-avatar")].map((a) => a.getAttribute("data-role-key")))
      .toEqual(["adversarial_reviewer", "security_reviewer"]);
  });

  it("a repair loop carries its rounds on the edge", async () => {
    await mount(view(one(ROWS.repairing)));
    expect($("[data-testid=fx-build-row-crun_rp] [data-key=build] .fx-rounds")?.textContent).toBe("×2");
  });

  it("cost shows only when metered", async () => {
    await mount(view(floor({ attention: [], builds: [ROWS.implementing, ROWS.queued], outcomes: [], stopped: [] })));
    expect($("[data-testid=fx-build-row-crun_inv2] .fx-row-cost")?.textContent).toBe("$0.31");
    expect($("[data-testid=fx-build-row-crun_inv2] .fx-row-cost")?.getAttribute("title")).toBe("Metered model spend so far");
    expect($("[data-testid=fx-build-row-crun_q] .fx-row-cost")?.textContent).toBe("");
  });

  it("past eight rows says +n more building", async () => {
    const rows = Array.from({ length: 11 }, (_, i) => ({ ...ROWS.implementing, run_id: `crun_m${i}`, app: `App ${i}` }));
    await mount(view(floor({ attention: [], builds: rows })));
    expect($$(".fx-row[data-testid^=fx-build-row-]")).toHaveLength(8);
    expect($("[data-testid=fx-more-building]")?.textContent).toBe("+3 more building");
  });

  it("past twenty rows groups them by app", async () => {
    const rows = Array.from({ length: 22 }, (_, i) => ({ ...ROWS.implementing, run_id: `crun_g${i}`, app: i < 4 ? "Team inbox" : `App ${i}` }));
    await mount(view(floor({ attention: [], builds: rows })));
    expect($("[data-testid=fx-group] .fx-group-head")?.textContent).toBe("Team inbox ×4");
  });
});

describe("the other sections", () => {
  it("shipped chips name the builder, or BOSS for a deploy", async () => {
    await mount(view(floor()));
    const chips = $$("[data-testid=fx-outcome]");
    expect(chips.map((c) => c.querySelector(".ag-out-text")?.textContent)).toEqual(["PR #842 merged", "Deployed · dev", "PR #839 opened"]);
    expect(chips[1].querySelector("[data-variant=boss]")).not.toBeNull();
  });

  it("a stopped row says why, with the stuck face and Retry", async () => {
    await mount(view(floor()));
    const row = $("[data-testid=fx-stopped-crun_f]")!;
    expect(row.querySelector(".fx-word")?.textContent).toBe("Failed · Build");
    expect(row.querySelector("[data-state=stuck]")).not.toBeNull();
    expect(row.querySelector<HTMLButtonElement>("[data-testid=fx-retry-crun_f]")?.disabled).toBe(false);
  });

  it("Retry is disabled with its reason when the server says it cannot help", async () => {
    await mount(view(floor({ stopped: [{ run_id: "crun_x", app: "Search", title: "t", state: "cancelled", word: "Stopped by you",
                                          retryable: false, retry_reason: "Nothing to resume", role_key: null, at: "2026-10-04T08:00:00Z" }] })));
    const btn = $("[data-testid=fx-retry-crun_x]") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe("Nothing to resume");
    expect($("[data-testid=fx-stopped-crun_x] .fx-word")?.textContent).toBe("Stopped by you");
  });
});

describe("the page's own states", () => {
  it("loading is a skeleton, busy, saying Loading", async () => {
    await mount(view(null, { loading: true }));
    expect($("[data-testid=fx-loading]")?.getAttribute("aria-busy")).toBe("true");
    expect($("[data-testid=fx-loading] .ag-sr")?.textContent).toBe("Loading");
  });

  it("an unreachable API says so, with Retry", async () => {
    await mount(view(null, { error: "connect ECONNREFUSED" }));
    expect($("[data-testid=fx-unreachable]")?.textContent).toContain("Can't reach BOSS");
    expect($("[data-testid=fx-unreachable] [title]")?.getAttribute("title")).toBe("connect ECONNREFUSED");
  });

  it("no builds ever: Plan it with Juno, or a Direct change", async () => {
    await mount(view(emptyFloor()));
    expect($("[data-testid=fx-no-builds]")?.textContent).toBe("No builds yetPlan it with JunoDirect change");
  });

  it("Paused says so and holds every face still", async () => {
    await mount(view(floor(), { paused: true }));
    expect($("[data-testid=fx-paused]")?.textContent).toBe("Paused");
    expect($$("[data-testid^=fx-build-row-]").every((r) => r.hasAttribute("data-still"))).toBe(true);
  });
});
