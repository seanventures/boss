import { describe, expect, it } from "vitest";

import { codeOnly, cssOnly } from "./_helpers/source";

/**
 * Ten boxes above, one panel below — which box the panel is about has to be visible.
 *
 *   "workflow steps should be boxed / When we click on each box the details should be
 *    displayed as a callout format"
 *
 * They were already bordered; what was missing was the line between the box and its detail.
 * That connection was carried only by a border colour on a step that might be on either row
 * of a wrapping rail.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = cssOnly("app/globals.css");

describe("the step rail", () => {
  it("never wraps: the run is one row that scrolls inside itself", () => {
    // REWRITTEN (Workflow Studio). The rail wrapped to a second row, so a step's place in the
    // flow depended on the window width. The run is now the shared lane: one row, placed by
    // geometry, that fits itself and scrolls sideways inside its own box only. Its node size and
    // two-line titles are pinned in tests/flow-lane.test.tsx.
    const FLOW_CSS = cssOnly("app/workflows/flow.css");
    const rule = FLOW_CSS.slice(FLOW_CSS.indexOf(".wf-lane-viewport {"), FLOW_CSS.indexOf(".wf-lane-viewport {") + 200);
    expect(rule).toContain("overflow-x: auto");
    // Every step is placed absolutely from computed columns, so there is nothing to wrap.
    expect(FLOW_CSS).toContain(".wf-slot { position: absolute;");
  });
});

describe("the detail reads as a callout", () => {
  it("draws a notch above the panel", () => {
    expect(CSS).toContain(".dw-detail::before");
    expect(CSS).toContain(".dw-detail::after");
    expect(CSS).toContain("border-bottom-color");
  });

  it("keeps the panel outline unbroken behind the notch", () => {
    // Two triangles: the outer paints the border, the inner sits a pixel inside and paints
    // the fill.
    // Anchored on the newline, because the combined `::before, ::after` selector contains
    // ".dw-detail::after {" as a substring and an unanchored indexOf lands on that instead.
    const at = CSS.indexOf("\n.dw-detail::after {");
    expect(at).toBeGreaterThan(-1);
    expect(CSS.slice(at, at + 260)).toContain("margin-bottom: -1px");
  });

  it("points at the step that was clicked, measured rather than computed", () => {
    // A position computed from an index is right until a label wraps to two lines or the rail
    // reflows — and then it points at the wrong box while looking just as confident.
    // Found by the step's own id in the lane, not by an index.
    expect(UI).toContain("lane.querySelector<HTMLElement>(`[data-step-id=\"${CSS.escape(picked)}\"]`)");
    expect(UI).toContain("--dw-arrow-x");
  });

  it("measures in the panel's own coordinates, not the offset parent's", () => {
    // `offsetLeft` is relative to whichever ancestor happens to be positioned, which is not
    // the panel — so the notch landed at an offset that looked plausible and pointed at the
    // wrong step.
    expect(UI).toContain("node.getBoundingClientRect()");
    expect(UI).toContain("panel.getBoundingClientRect()");
    expect(UI).toContain("box.left + box.width / 2 - frame.left");
    expect(UI).not.toContain("node.offsetLeft");
  });

  it("never lets the notch hang off the end of the panel", () => {
    // A notch past the rounded corner reads as a rendering bug rather than as a pointer.
    expect(UI).toContain("Math.max(18, Math.min(frame.width - 18, centre))");
  });

  it("re-measures when the rail reflows, or scrolls inside itself", () => {
    expect(UI).toContain('window.addEventListener("resize", place)');
    expect(UI).toContain('window.removeEventListener("resize", place)');
    expect(UI).toContain('viewport?.addEventListener("scroll", place, { passive: true })');
    expect(UI).toContain('viewport?.removeEventListener("scroll", place)');
  });

  it("centres the notch when nothing has been measured", () => {
    // Also what a printed page gets, where no layout effect has run.
    expect(CSS).toContain("var(--dw-arrow-x, 50%)");
  });

  it("colours the notch with the step's own state", () => {
    for (const phase of ["now", "findings", "failed"]) {
      expect(CSS).toContain(`.dw-detail[data-phase="${phase}"]::before`);
    }
  });
});

describe("the run is named, not quoted", () => {
  it("puts the run title through the namer the rest of the console uses", () => {
    // `input.title` is usually the directive already truncated for display — ellipsis and all
    // — so the header showed a broken sentence while the report below it called the same app
    // "Incident Timeline Builder". Two names for one thing on one screen.
    expect(UI).toContain("appIdentity(run.input as AppRun).name || raw");
  });
});

describe("Settings names the page for what is in it", () => {
  it("calls it Repo and models, first in Settings", () => {
    // "Setup" is a stage of the product's life: it reads as something done once, so the page
    // an operator needs when a token expires was filed under a name that says "already done".
    const SHELL = codeOnly("app/ConsoleShell.tsx");
    const BOARD = codeOnly("app/setup/setup-board.tsx");
    expect(SHELL).toContain('label: "Repo and models"');
    expect(SHELL.indexOf('{ id: "setup"')).toBeLessThan(SHELL.indexOf('{ id: "skills"'));
    expect(BOARD).toContain("<h1>Repo and models</h1>");
    expect(codeOnly("app/page.tsx")).toContain('aria-label="Repo and models"');
  });

  it("keeps the area id, which routing and the parity map are keyed on", () => {
    const SHELL = codeOnly("app/ConsoleShell.tsx");
    expect(SHELL).toContain('{ id: "setup"');
  });
});


describe("the lane reads as a chain", () => {
  it("connects the first step to the node before it", () => {
    // Agents and Research were the only pair in the rail with no line between them, because
    // "no earlier step" was treated as "nothing has flowed". On the shared lane the first
    // step's line comes from Start, which has always flowed (REWRITTEN).
    const LANE = codeOnly("app/workflows/flow/flow-lane.tsx");
    expect(LANE).toContain('index <= 0 ? "start"');
    expect(LANE).toContain('const passed = previous === "start"');
  });
});

describe("the opened step reads as one region", () => {
  it("gives the step's detail a body of its own", () => {
    // The panel, the run report and the management row all sat on the same ground at the same
    // weight: "I dont know what was expanded for that step".
    const at = CSS.indexOf(".dw-detail {");
    const rule = CSS.slice(at, at + 900);
    expect(rule).toContain("border-left: 3px solid");
    expect(rule).toContain("color-mix(in srgb, var(--accent");
  });

  it("tints rather than recolours, so the text is not re-read against a new background", () => {
    const at = CSS.indexOf(".dw-detail {");
    expect(CSS.slice(at, at + 900)).toMatch(/var\(--accent[^)]*\) 7%/);
  });

  it("separates that region from whatever follows it", () => {
    expect(CSS).toContain(".dw-detail-wrap { margin-bottom: 14px; }");
  });

  it("keeps the notch fill matching the panel fill", () => {
    // A notch painted in the old fill reads as a hole punched in the panel.
    const at = CSS.indexOf("\n.dw-detail::after {");
    expect(CSS.slice(at, at + 300)).toContain("var(--accent");
  });
});

describe("which run is expanded", () => {
  it("marks the open card with the accent at full strength", () => {
    // Mixed 45% into the border colour, against --surface-2, it landed a few percent from the
    // closed card's own border: the open card was marked by a shadow and almost nothing else.
    const at = CSS.indexOf('.dw-run[data-open="true"] {');
    expect(at).toBeGreaterThan(-1);
    const rule = CSS.slice(at, at + 400);
    expect(rule).toContain("border-color: var(--accent");
    expect(rule).not.toContain("var(--accent) 45%");
  });

  it("holds the other runs back so there is something to compare against", () => {
    // A list of equally-bright rows with one slightly brighter row is a spot-the-difference
    // puzzle. The same list with the others held back is unambiguous at a glance.
    expect(CSS).toContain('.dw-runs:has(.dw-run[data-open="true"]) .dw-run:not([data-open="true"])');
    expect(CSS).toMatch(/opacity: \.7\d?;/);
  });

  it("applies that only while something is open", () => {
    // Scoped to :has, so the list returns to normal when the card closes.
    const at = CSS.indexOf('.dw-runs:has(.dw-run[data-open="true"])');
    expect(CSS.slice(0, at)).not.toContain(".dw-run:not([data-open");
  });

  it("brings a held-back run back on hover or focus", () => {
    // The one you want next is often one of the quiet ones, and it must never be unreadable
    // or unreachable by keyboard.
    expect(CSS).toContain(':not([data-open="true"]):hover');
    expect(CSS).toContain(':not([data-open="true"]):focus-within');
  });

  it("keeps the status colour on the left edge, which answers a different question", () => {
    // "which run is this" and "this is the one I opened" are two signals, not one.
    expect(CSS).toContain('.dw-runs .dw-run[data-open="true"] { border-left-width: 4px; }');
    expect(CSS).toContain('.dw-runs .dw-run[data-status="waiting"]');
  });
});
