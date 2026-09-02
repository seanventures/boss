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

describe("you can say something to an agent", () => {
  const WF = codeOnly("app/workforce-ui.tsx");
  const CHAT = codeOnly("app/agent-ui.tsx");

  it("every agent card offers it", () => {
    // The page listed what every agent had done and offered no way to say anything to any of
    // them — read-only, on a screen about a workforce.
    expect(WF).toContain('data-testid={`wf-ask-${a.id}`}');
    expect(WF).toContain("function askHref");
  });

  it("opens the chat already addressed", () => {
    // So asking is a click from the card that named them, not navigating here and retyping
    // who you meant.
    expect(WF).toContain('next.set("area", "agent")');
    expect(WF).toContain("`@${agentName}: `");
    expect(CHAT).toContain('get("ask")');
  });

  it("consumes the instruction rather than leaving it in the URL", () => {
    // A one-shot left in the URL is re-delivered by every later navigation, which is how a
    // composer ends up refilling itself behind your typing.
    expect(CHAT).toContain('url.searchParams.delete("ask")');
  });

  it("keeps who you are when it navigates", () => {
    expect(WF).toContain('for (const key of ["org", "org_id"])');
  });
});

describe("the Engineering Captain leads the work", () => {
  const CT = codeOnly("../../engine/boss_engine/services/code_task_service.py");
  const PS = codeOnly("../../engine/boss_engine/services/project_settings.py");

  it("is put on a new project, not only a benchmark run", () => {
    // The captain existed, appeared on the org chart, and did nothing: the swarm that puts it
    // in charge was bound only for official runs, so every ordinary project was built by an
    // implementer with nobody above it.
    expect(CT).toContain("bind_captain_swarm");
    expect(CT).toContain("official=False");
  });

  it("is a knob, because leading a project costs turns", () => {
    expect(PS).toContain('"captain_leads_projects"');
    expect(CT).toContain('"captain_leads_projects"');
  });

  it("never stops a build from happening", () => {
    // An unled build is better than no build.
    expect(CT).toContain("could not put a captain on");
  });
});

describe("the composer looks like the console it is in", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");
  const CSS_N = codeOnly("app/globals.css");
  const DS = codeOnly("app/design-system.css");

  it("uses a class the stylesheet actually defines", () => {
    // `mr-input` was invented and defined nowhere, so every field wearing it rendered as a
    // browser-default white box in a dark console. The design system defines .input and
    // .textarea.
    expect(DW).not.toContain('className="mr-input"');
    expect(DS).toContain(".textarea,");
  });

  it("puts the field and its controls on one surface", () => {
    // Three separate elements sharing a row read as a form; the Command Center's composer is
    // the same act from another page and looks like one thing.
    expect(CSS_N).toContain(".dw-new-box {");
    expect(CSS_N).toContain(".dw-new-box:focus-within");
  });

  it("starts on the shortcut every other composer uses", () => {
    expect(DW).toContain("(e.metaKey || e.ctrlKey) && e.key === \"Enter\"");
  });
});
