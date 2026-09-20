import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const SHELL = readFileSync(join(here, "../app/ConsoleShell.tsx"), "utf8");
const CSS = readFileSync(join(here, "../app/globals.css"), "utf8");

/**
 * "I want to collapse the left side pane like a dock panel, but when I collapse I should see
 * the Icons so I can still use that for navigation."
 *
 * A dock, not a disappearance: hiding the sidebar trades screen width for having to remember
 * what was in it. Every destination stays one click away and in the same position, so muscle
 * memory survives the collapse — which is the only reason to dock rather than hide.
 */
describe("the sidebar docks to icons", () => {
  it("has a control to collapse it", () => {
    expect(SHELL).toContain('data-testid="nav-dock-toggle"');
    expect(SHELL).toContain("aria-pressed={docked}");
  });

  it("says which way it will go, for a screen reader too", () => {
    expect(SHELL).toContain("Collapse the sidebar to icons");
    expect(SHELL).toContain("Expand the sidebar");
  });

  it("keeps the preference, because a width that resets every visit is not a preference", () => {
    expect(SHELL).toContain('localStorage.getItem("boss.nav.docked")');
    expect(SHELL).toContain('localStorage.setItem("boss.nav.docked"');
  });

  it("survives private mode, where localStorage throws rather than returning null", () => {
    const read = SHELL.slice(SHELL.indexOf('localStorage.getItem("boss.nav.docked")'));
    expect(read.slice(0, 200)).toContain("catch");
  });

  it("narrows the shell instead of hiding the column", () => {
    expect(CSS).toContain(".bx-shell.nav-docked { grid-template-columns: 68px");
  });

  it("keeps the icons — the labels are what go", () => {
    const docked = CSS.slice(CSS.indexOf(".bx-shell.nav-docked .bx-brand-copy"));
    const block = docked.slice(0, docked.indexOf("}") + 1);
    expect(block).toContain(".bx-nav-item span");     // the label
    expect(block).not.toContain(".bx-nav-item {");    // never the item itself
    expect(block).toContain("display: none");
  });

  it("gives the name back on hover, so icons are not a memory test", () => {
    expect(SHELL).toContain("data-label={labelFor(a)}");
    expect(CSS).toContain("content: attr(data-label)");
  });

  it("leaves the mobile drawer alone rather than having two collapsed states disagree", () => {
    const mobile = CSS.slice(CSS.lastIndexOf("@media (max-width: 900px)"));
    expect(mobile).toContain(".bx-shell.nav-docked { grid-template-columns: 256px");
    expect(mobile).toContain(".bx-dock-toggle { display: none; }");
  });
});
