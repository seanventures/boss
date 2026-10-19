// @vitest-environment happy-dom
/**
 * AN OVERLAY COVERS THE SCREEN, NOT ITS PANEL. A `position: fixed` drawer inside a transformed
 * ancestor (a panel that animates in) is fixed to that panel: Expand filled two-thirds of the
 * viewport. Every Drawer and Sheet renders at the end of <body>; an expanded drawer is the whole
 * screen under the shell header; Expand and × sit side by side and never overlap.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { describe, expect, it, vi } from "vitest";

import { $, click, mount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(window.location.search) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { Drawer, Sheet } = await import("../app/infra/kit");

const css = readFileSync(join(process.cwd(), "app/infra/infra.css"), "utf8");
const rule = (sel: string) => css.match(new RegExp(`${sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`))?.[1] ?? "";

describe("an overlay covers the screen", () => {
  it("a Drawer and a Sheet render at the end of <body>, outside a transformed panel", async () => {
    await mount(
      <section className="panel-stack" style={{ transform: "translateY(0)" }} data-testid="panel">
        <Drawer label="x" testId="d" expandable onClose={() => {}} head={<span>App</span>}>body</Drawer>
        <Sheet title="s" testId="s" onClose={() => {}}>form</Sheet>
      </section>,
    );
    for (const id of ["d", "s"]) {
      const el = $(`[data-testid="${id}"]`);
      expect(el.closest("[data-testid=panel]")).toBeNull();
      expect(el.parentElement?.parentElement).toBe(document.body);
    }
  });

  it("Expand makes the drawer the whole screen under the shell header", async () => {
    await mount(<Drawer label="x" testId="d" expandable onClose={() => {}} head={<span>App</span>}>body</Drawer>);
    await click($("[data-testid=d-expand]"));
    expect($("[data-testid=d]").parentElement?.hasAttribute("data-expanded")).toBe(true);
    expect(rule(".inf-drawer-back[data-expanded]")).toMatch(/top:\s*var\(--shell-head,\s*62px\)/);
    expect(rule(".inf-drawer[data-expanded]")).toMatch(/width:\s*100%/);
    expect(rule(".inf-drawer")).toMatch(/height:\s*100%/);
  });

  it("Expand is a text button that keeps its width beside ×", async () => {
    await mount(<Drawer label="x" testId="d" expandable onClose={() => {}} head={<span>App</span>}>body</Drawer>);
    const ex = $("[data-testid=d-expand]");
    expect(ex.classList.contains("ag-icon-btn")).toBe(false); // a fixed 30 px box "Expand" spilled out of
    expect(ex.nextElementSibling?.getAttribute("aria-label")).toBe("Close");
    expect(css).toMatch(/\.inf-drawer-head > \.inf-drawer-expand, \.inf-drawer-head > \.ag-act-close \{[^}]*flex: none/);
  });
});
