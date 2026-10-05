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
  it("has a control to collapse it, and it says the state the sidebar is really in", () => {
    expect(SHELL).toContain('data-testid="nav-dock-toggle"');
    // Railed = the operator collapsed it, OR the open dock folded it to make room
    // (lib/shell-rail.ts); the control reports what is on screen, not only the preference.
    expect(SHELL).toContain("const railed = docked || (railForChat && !keptOpen);");
    expect(SHELL).toContain("aria-pressed={railed}");
    expect(SHELL).toContain('${railed ? " nav-docked" : ""}');
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
    // The WIDTH is a design choice that will change; that the column still exists, and is
    // narrower than the expanded one, is the contract. Pinning the pixel value made a
    // legitimate spacing fix look like a regression.
    const docked = CSS.match(/\.bx-shell\.nav-docked \{ grid-template-columns: (\d+)px/);
    const expanded = CSS.match(/\.bx-shell \{[^}]*grid-template-columns: (\d+)px/s);
    expect(docked).not.toBeNull();
    expect(expanded).not.toBeNull();
    const dockedPx = Number(docked![1]);
    expect(dockedPx).toBeGreaterThan(48);                 // room for an icon and its padding
    expect(dockedPx).toBeLessThan(Number(expanded![1]));  // and still a collapse
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
