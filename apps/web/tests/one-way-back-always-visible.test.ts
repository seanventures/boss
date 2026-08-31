import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * One way back, and it does not vanish when you tidy the pane.
 *
 * The detail screen carried TWO back controls with different names and identical behaviour:
 * "← All apps" inside the dockable run list, and "← All code tasks" further down the detail.
 * Both called `setSelected(null)`.
 *
 * The first one lived inside the thing that can be hidden, so docking the list took the way
 * back with it — and the workaround for THAT was to drop the label when docked, leaving an
 * unexplained "»" as the only control on the rail. A back button that disappears when you
 * collapse a pane is a trap; a second one under a different name is not a fix for it.
 */
const SRC = codeOnly("app/code-tasks-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("there is exactly one", () => {
  it("keeps a single back control", () => {
    const backs = SRC.match(/←<\/span> All (projects|apps|code tasks)/g) ?? [];
    expect(backs).toHaveLength(1);
  });

  it("drops the duplicate that said 'All code tasks'", () => {
    expect(SRC).not.toContain('className="mr-back"');
  });
});

describe("it survives docking", () => {
  it("lives in the detail column, which never docks", () => {
    // Not in `.ct-split-list` — that pane collapses to a 44px rail, and a back button that
    // disappears when you tidy the pane is a trap.
    const head = SRC.slice(SRC.indexOf('className="ct-detail-head"'),
                           SRC.indexOf('<h3>{run.title}</h3>'));
    expect(head).toContain('data-testid="ct-detail-back"');
  });

  it("is not rendered conditionally on the dock state", () => {
    const head = SRC.slice(SRC.indexOf('className="ct-detail-head"'),
                           SRC.indexOf('<h3>{run.title}</h3>'));
    expect(head).not.toContain("listDocked");
  });

  it("costs no row of its own", () => {
    // It had one — a whole line of vertical space for one button, on a page whose header
    // already spends four lines before any content.
    expect(SRC).not.toContain("ct-split-top");
    expect(CSS).not.toContain(".ct-split-top");
  });

  it("is styled as a back control, not another flat chip", () => {
    expect(CSS).toContain(".ct-back");
    expect(CSS).toContain(".ct-back:hover span");   // the arrow moves
  });

  it("leaves only the dock toggle in the list bar", () => {
    const bar = SRC.slice(SRC.indexOf('className="ct-split-listbar"'),
                          SRC.indexOf('className="ct-split-items"'));
    expect(bar).toContain("ct-dock-toggle");
    expect(bar).not.toContain("ct-detail-back");
  });
});
