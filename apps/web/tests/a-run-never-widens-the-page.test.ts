/**
 * A run's lane scrolls inside itself; it never widens the console. The runs list was an `auto`
 * grid column, so an 11-step lane sized the column — and the page — to ~2,500px on boss-1, and
 * the callout notch measured against that width stayed out at 1,869px (2026-10-03).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(join(__dirname, "../app/globals.css"), "utf8");
const UI = readFileSync(join(__dirname, "../app/delivery-workflows-ui.tsx"), "utf8");

describe("a run never widens the page", () => {
  it("gives the runs list a column that can shrink below its content", () => {
    expect(CSS).toMatch(/\.dw-runs \{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    expect(CSS).toMatch(/\.dw-runs > \* \{\s*min-width:\s*0;/);
  });

  it("hides the callout notch when its step is scrolled out of the lane's view", () => {
    expect(UI).toContain('panel.setAttribute("data-arrow", "off")');
    expect(CSS).toContain('.dw-detail[data-arrow="off"]::before');
  });
});
