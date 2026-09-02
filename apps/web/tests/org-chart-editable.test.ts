import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The chart was derived entirely from observed handoffs — who passed work to whom in the last
 * week. Honest, useful, and completely unchangeable: nobody could say "the QA lead reports to
 * the captain" or add a seat that has not been filled yet. A measurement nobody can act on is a
 * report, and this one had no effect anywhere else in the product.
 */
const UI = codeOnly("app/workforce-ui.tsx");
const SVC = codeOnly("../../engine/boss_engine/services/org_assignments.py");
const CSS = codeOnly("app/globals.css");

describe("the chart can be changed", () => {
  it("every seat can be dragged onto another", () => {
    expect(UI).toContain("onDragStart={() => setDragging(n.id)}");
    expect(UI).toContain("if (dragging && dragging !== n.id) void place(dragging, n.id);");
  });

  it("offers the agents nobody has placed yet", () => {
    // The list of agents BOSS knows about is the thing you build a chart FROM; without it the
    // only agents you could place would be the ones already placed.
    expect(UI).toContain('data-testid="org-bench"');
    expect(UI).toContain("Not placed yet");
  });

  it("can make a seat a root, and hand one back", () => {
    expect(UI).toContain("void place(n.id, null)");
    expect(UI).toContain('data-testid={`org-reset-${n.id}`}');
    expect(UI).toContain('data-testid="org-reset-all"');
  });
});

describe("the declared chart never destroys the observed one", () => {
  it("keeps both", () => {
    // Where they disagree is the most interesting thing on the page: an agent meant to report
    // to the captain who in practice hands everything to the architect.
    expect(SVC).toContain("def apply_to");
    expect(SVC).toContain('"declared":');
    expect(SVC).toContain('"has_declared": False');
    expect(SVC).toContain('"has_declared": True');
  });

  it("marks which seats were decided rather than measured", () => {
    expect(SVC).toContain('node["placed"] = True');
    expect(CSS).toContain('.org-node[data-placed="true"]');
  });

  it("keeps a seat somebody placed even if BOSS has never seen it work", () => {
    // A seat created and not yet filled: dropping it would make the editor appear to forget
    // what it was told.
    expect(SVC).toContain('"status": "UNSEEN"');
  });
});

describe("a chart that cannot be drawn is refused before it is stored", () => {
  it("catches a loop, however many hops", () => {
    // A cycle is not a display bug: the renderer would recurse forever or silently drop
    // somebody.
    expect(SVC).toContain("class OrgCycle");
    expect(SVC).toContain("def _would_cycle");
    expect(SVC).toContain("already sits above");
  });

  it("catches the one-hop case too", () => {
    expect(SVC).toContain("cannot report to itself");
  });

  it("distinguishes 'is a root' from 'nobody has said'", () => {
    // A NULL parent is a real statement; deleting is how a seat goes back to being observed.
    expect(SVC).toContain("def clear_one");
    expect(SVC).toContain("DELETE FROM org_assignments");
  });
});
