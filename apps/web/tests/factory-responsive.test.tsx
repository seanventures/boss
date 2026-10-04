// @vitest-environment happy-dom
/**
 * The Floor at a phone's width (factory spec §5.3): the same section order; a build row is two
 * lines with only the current face; under 1024px the line folds to `✓k · face · n next`; touch
 * targets are 44px; nothing is laid out wider than the column.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseFloor } from "../lib/factory-floor";
import { codeOnly } from "./_helpers/source";
import { floor } from "./_helpers/factory-fixtures";
import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { FloorView } = await import("../app/factory/floor");
const CSS = codeOnly("app/factory/factory.css");
const PHONE = CSS.slice(CSS.indexOf("{", CSS.indexOf("@media (max-width: 639px)")), CSS.indexOf("@media (prefers-reduced-motion"));

afterEach(() => unmount());
const noop = () => {};
const at = (band: "phone" | "narrow") => (
  <FloorView projectId="p1" actor="ceo" floor={parseFloor(floor())} error={null} loading={false} refresh={noop}
             onOpen={noop} onPlan={noop} onDirect={noop} onApps={noop} band={band} />
);

describe("at 390px", () => {
  it("keeps the order: Needs you, Building, Shipped, Stopped", async () => {
    await mount(at("phone"));
    expect($$(".ag-live-sec").map((s) => s.getAttribute("data-sec"))).toEqual(["needs", "building", "shipped", "stopped"]);
  });

  it("a row's line is the current face only", async () => {
    await mount(at("phone"));
    const line = $("[data-testid=fx-build-row-crun_inv2] .fx-line")!;
    expect(line.classList.contains("fx-line-folded")).toBe(true);
    expect(line.querySelectorAll(".ag-avatar")).toHaveLength(1);
    expect(line.querySelector("[data-key=build]")).not.toBeNull();
    expect(line.textContent).not.toMatch(/next|✓/);
  });

  it("rows are two lines, buttons 44px, no cost column", () => {
    expect(PHONE).toContain('grid-template-areas: "name line" "now go"');
    expect(PHONE).toMatch(/\.fx-row-go[^{]*\{[^}]*min-height: 44px/);
    expect(PHONE).toContain(".fx-row-cost { display: none; }");
    expect(PHONE).toMatch(/\.fx-empty \.ag-btn \{ flex: 1 1 100%/);
  });

  it("columns can shrink: no fixed width wider than the phone", () => {
    for (const m of PHONE.matchAll(/(?:min-)?width:\s*(\d+)px/g)) expect(Number(m[1])).toBeLessThanOrEqual(358);
  });
});

describe("under 1024px", () => {
  it("folds the line to done · current face · next", async () => {
    await mount(at("narrow"));
    const line = $("[data-testid=fx-build-row-crun_inv2] .fx-line")!;
    expect(line.querySelector(".fx-fold-done")?.textContent).toBe("✓2");
    expect(line.querySelector(".fx-fold-next")?.textContent).toBe("5 next");
  });
});
