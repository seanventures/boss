// @vitest-environment happy-dom
/**
 * The Floor says what needs you first (factory spec §4.2): NEEDS YOU, BUILDING, SHIPPED,
 * STOPPED, in that order. A PR card never merges in place (`Review ↗`, plus `Open PR ↗`); a
 * checks-failed card offers `Retry` and `Waive…`; the waiver sheet cannot waive `App starts`,
 * says why, and will not send without a reason.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { parseFloor } from "../lib/factory-floor";
import { floor } from "./_helpers/factory-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

const waive = vi.fn(async (_form: FormData) => ({ ok: true, url: "https://github.com/acme/app/pull/843" }));
const retry = vi.fn(async (_form: FormData) => ({ ok: true, runId: "crun_cf2", attempt: 3 }));
vi.mock("../app/factory/floor-actions", () => ({
  waiveChecksAction: (form: FormData) => waive(form),
  retryBuildAction: (form: FormData) => retry(form),
  startDirectChangeAction: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { FloorView } = await import("../app/factory/floor");

const noop = () => {};
const view = (body = floor()) => (
  <FloorView projectId="p1" actor="ceo" floor={parseFloor(body)} error={null} loading={false} refresh={noop}
             onOpen={noop} onPlan={noop} onDirect={noop} onApps={noop} band="wide" />
);

let toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);
beforeEach(() => { waive.mockClear(); retry.mockClear(); toasts = []; window.addEventListener("boss:toast", onToast); });
afterEach(() => { window.removeEventListener("boss:toast", onToast); unmount(); });

describe("the Floor's order", () => {
  it("is NEEDS YOU, BUILDING, SHIPPED, STOPPED", async () => {
    await mount(view());
    expect($$(".ag-live-sec .ag-zone-label").map((h) => h.textContent)).toEqual(["NEEDS YOU", "BUILDING", "SHIPPED", "STOPPED"]);
  });

  it("says each section is empty in one line, and keeps its place", async () => {
    await mount(view(floor({ attention: [], builds: [], outcomes: [], stopped: [], counts: { building: 0, needs_you: 0, shipped_7d: 0, stopped: 0, apps: 2 } })));
    expect($("[data-testid=fx-nothing-needs]")?.textContent).toBe("Nothing needs you");
    expect($("[data-testid=fx-nothing-building]")?.textContent).toBe("Nothing building");
    expect($("[data-testid=fx-nothing-shipped]")?.textContent).toBe("Nothing shipped this week");
    expect($("[data-testid=fx-nothing-stopped]")?.textContent).toBe("Nothing stopped");
  });
});

describe("the cards", () => {
  it("a PR card is Review ↗, never merged in place, and opens the PR", async () => {
    await mount(view());
    const card = $("[data-testid='ag-att-dec_pr842']")!;
    expect(card.querySelector("[data-testid=ag-att-review]")?.textContent).toContain("Review ↗");
    expect(card.querySelector("[data-testid=ag-att-approve]")).toBeNull();
    expect(card.querySelector("[data-testid=ag-att-open-pr]")?.getAttribute("href")).toBe("https://github.com/acme/app/pull/842");
    expect(card.querySelector("[data-testid=ag-att-build]")?.textContent).toBe("Invoice export · PR open");
  });

  it("a plan card shows the architect's face and approves in place", async () => {
    await mount(view());
    const card = $("[data-testid=ag-att-dec_plan1]")!;
    expect(card.querySelector(".ag-att-title")?.textContent).toBe("Approve the plan?");
    expect(card.querySelector("[data-role-key=chief_architect]")).not.toBeNull();
    expect(card.querySelector("[data-testid=ag-att-approve]")).not.toBeNull();
    await click(card.querySelector("[data-testid=ag-att-details]"));
    expect(card.querySelector("[data-testid=ag-att-open-plan]")?.textContent).toContain("Open plan ↗");
  });

  it("a checks-failed card offers Retry and Waive…", async () => {
    await mount(view());
    const card = $("[data-testid='ag-att-build:crun_cf:gates']")!;
    expect(card.querySelector(".ag-att-title")?.textContent).toBe("2 checks failed");
    expect(card.querySelector("[data-testid=ag-att-build]")?.textContent).toBe("Team inbox · Review · round 2");
    expect(card.querySelector("[data-testid=ag-att-retry]")?.textContent).toBe("Retry");
    expect(card.querySelector("[data-testid=ag-att-waive]")?.textContent).toBe("Waive…");
    expect(card.querySelector("[data-testid=ag-att-waive]")?.getAttribute("title")).toBe("Open the PR anyway. The merge still needs you.");
    await click(card.querySelector("[data-testid=ag-att-retry]"));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(retry.mock.calls[0][0].get("run_id")).toBe("crun_cf");
    expect(toasts).toContain("Attempt 3 started");
  });

  it("the waiver cannot waive App starts, and needs a reason", async () => {
    await mount(view());
    await click($("[data-testid=ag-att-waive]"));
    const sheet = $("[data-testid=fx-waive-sheet]")!;
    const serves = sheet.querySelector<HTMLInputElement>("[data-testid=fx-waive-app_serves]")!;
    expect(serves.disabled).toBe(true);
    expect(serves.closest("label")?.getAttribute("title")).toBe("Can't be waived: the app must start");
    const submit = sheet.querySelector<HTMLButtonElement>("[data-testid=fx-waive-submit]")!;
    expect(submit.textContent).toContain("Waive and open PR");
    expect(submit.disabled).toBe(true);
    const reason = sheet.querySelector<HTMLTextAreaElement>("[data-testid=fx-waive-reason]")!;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(reason, "Reviewed by hand");
    reason.dispatchEvent(new Event("input", { bubbles: true }));
    await settle();
    expect(submit.disabled).toBe(false);
    await click(submit);
    expect(waive).toHaveBeenCalledTimes(1);
    const sent = waive.mock.calls[0][0];
    expect(sent.getAll("gate")).toEqual(["code_review_passed", "security_clean"]);
    expect(sent.get("reason")).toBe("Reviewed by hand");
    expect(toasts).toContain("2 checks waived · opening PR");
  });

  it("a budget card offers Raise budget ↗ and Retry, with the BOSS glyph", async () => {
    const { attentionBudget } = await import("./_helpers/factory-fixtures");
    await mount(view(floor({ attention: [attentionBudget()] })));
    const card = $("[data-testid='ag-att-build:crun_br:budget']")!;
    expect(card.querySelector("[data-variant=boss]")).not.toBeNull();
    expect(card.querySelector("[data-testid=ag-att-raise-budget]")?.textContent).toBe("Raise budget ↗");
    expect(card.querySelector("[data-testid=ag-att-retry]")).not.toBeNull();
  });
});
