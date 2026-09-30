/**
 * The agent detail view is portalled, and I got this wrong twice.
 *
 * `.bx-shell` carries `isolation: isolate` and the console renders more than one shell (Next
 * streams the tree into a hidden <div id="S:0"> holding a second complete one). A
 * `position: fixed` element inside one of them cannot paint above anything in the other, at any
 * z-index — the template page was covered by the project strip, and the agent detail, which
 * reused the same `.tpl-page` class WITHOUT the portal, rendered clipped to 260px with all 38
 * of its rows present and invisible.
 *
 * A DOM query said "rendered: true, appRows: 38". A screenshot said otherwise. The class is not
 * what fixes this; leaving the shell is, and any future full-screen view has the same trap
 * waiting for it.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const DETAIL = codeOnly("app/agent-detail-ui.tsx");
const CSS = codeOnly("app/globals.css");

describe("a full-screen view has to leave the shell", () => {
  it("the agent detail is portalled to the body", () => {
    expect(DETAIL).toContain("createPortal(page, document.body)");
  });

  it("and never built during a server render", () => {
    expect(DETAIL).toContain('typeof document !== "undefined"');
  });

  it("every user of .tpl-page portals, or the trap catches the next one too", () => {
    // The rule, stated once: if a file positions itself with `.tpl-page` it must also portal.
    for (const file of ["app/agent-detail-ui.tsx", "app/ask-boss-hero.tsx"]) {
      const src = codeOnly(file);
      if (!src.includes('className="tpl-page"')) continue;
      expect(src, file).toContain("createPortal");
    }
  });

  it("and the trap is still there, which is why the rule exists", () => {
    expect(CSS).toContain("isolation: isolate");
  });
});

describe("the detail view says what it cannot show", () => {
  it("an agent with no spec is told, not left blank", () => {
    // An unspecced agent has nothing constraining what it attempts; a blank panel reads as
    // "nothing to say about this one".
    expect(DETAIL).toContain("This agent has no spec");
    expect(DETAIL).toContain("nothing constrains what it will attempt");
  });

  it("an unregistered agent is named as ungoverned", () => {
    expect(DETAIL).toContain("no registered role");
  });

  it("assigned-to and worked-on are shown as different questions", () => {
    // An agent assigned to an app it has never touched is idle or blocked; one working on an
    // app it is not assigned to gets refused at the next gate. Collapsing them hides both.
    expect(DETAIL).toContain("no work recorded yet");
    expect(DETAIL).toContain("refused at the gate");
  });

  it("and dates use the hydration-safe helper", () => {
    // toLocaleDateString renders different text on the server and in the browser, and React
    // tears the row out. The suite caught this one before it shipped.
    expect(DETAIL).not.toContain("toLocaleDateString");
    expect(DETAIL).toContain("formatDate(");
  });
});
