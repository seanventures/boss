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
    const backs = SRC.match(/←\s*All (apps|code tasks)/g) ?? [];
    expect(backs).toHaveLength(1);
  });

  it("drops the duplicate that said 'All code tasks'", () => {
    expect(SRC).not.toContain('className="mr-back"');
    // ...and the prop that existed only to feed it.
    expect(SRC).not.toContain("onBack");
  });
});

describe("it survives docking", () => {
  it("lives outside the dockable pane", () => {
    const top = SRC.slice(SRC.indexOf('className="ct-split-top"'),
                          SRC.indexOf('className="ct-split"'));
    expect(top).toContain('data-testid="ct-detail-back"');
  });

  it("is not rendered conditionally on the dock state", () => {
    // `{!listDocked ? <button…> : null}` was the bug: the control existed only while the pane
    // was open, which is precisely when you least need it.
    const top = SRC.slice(SRC.indexOf('className="ct-split-top"'),
                          SRC.indexOf('className="ct-split"'));
    expect(top).not.toContain("listDocked");
  });

  it("sits above the split, not inside either column", () => {
    expect(SRC.indexOf('className="ct-split-top"'))
      .toBeLessThan(SRC.indexOf('<div className="ct-split" data-docked'));
    expect(CSS).toContain(".ct-split-top");
  });

  it("leaves only the dock toggle in the list bar", () => {
    const bar = SRC.slice(SRC.indexOf('className="ct-split-listbar"'),
                          SRC.indexOf('className="ct-split-items"'));
    expect(bar).toContain("ct-dock-toggle");
    expect(bar).not.toContain("ct-detail-back");
  });
});
