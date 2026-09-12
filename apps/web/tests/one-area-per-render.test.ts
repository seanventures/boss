import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The console renders the area you asked for, and not the other thirty-nine.
 *
 * Measured on the live deployment before this: 8.3 MB of HTML and ~9s of server render on every
 * load, 78% of it an RSC payload carrying every area at once — Discovery 949 KB, Task Shepherd
 * 431 KB, Decisions 326 KB — all parsed and hydrated by the operator's machine, all but one
 * hidden by a CSS rule. An operator reported their laptop heating up during a video call.
 *
 * The data was never the cause: every feed the page needs answers in 83–235 ms.
 *
 * After: 25–235 KB per area, rendered in 30–40 ms (overview, the heaviest, 139 KB in 2.3s).
 */
const PAGE_RAW = readFileSync(join(process.cwd(), "app", "page.tsx"), "utf8");
const PAGE = codeOnly("app/page.tsx");
const SHELL = codeOnly("app/ConsoleShell.tsx");

describe("only the requested area is rendered", () => {
  it("there is one gate, derived from the URL", () => {
    expect(PAGE).toContain("const shows = (area: string) => area === shownArea;");
    expect(PAGE).toContain('requestedArea.trim() : "overview"');
  });

  it("every area section passes through it", () => {
    // 68 sections at the top level of the frame. A section added without a gate is a section
    // every other area pays for.
    const sections = PAGE_RAW.split("\n").filter((l) => /^        <section\b/.test(l)).length;
    const gates = (PAGE_RAW.match(/\{shows\("/g) ?? []).length;
    expect(sections).toBeGreaterThan(60);
    expect(gates).toBeGreaterThanOrEqual(sections);
  });

  it("including the components that carry their own area marker", () => {
    // These render `<section data-area>` inside themselves, so the gate goes on the mount.
    // Missed on the first pass and caught by measuring: a Discovery render still carried a
    // GitHub panel and a Govern panel.
    expect(PAGE).toContain('{shows("github") ? <RepoActivityPanel');
    expect(PAGE).toContain('{shows("budget") ? (\n          <TokenEfficiencyPanel');
  });
});

describe("which is only safe because switching now reaches the server", () => {
  it("the sidebar navigates rather than rewriting the URL", () => {
    // While it switched with pushState and no round-trip, whichever area the server happened to
    // render for decided what data existed for the whole session — so nothing could be left
    // out. That constraint is what produced the 8.3 MB page.
    const go = SHELL.slice(SHELL.indexOf("const go = useCallback"),
                           SHELL.indexOf("Quick-action links"));
    expect(go).toContain("router.push");
    // The COMMENT above it still names pushState, explaining what changed and why — that is
    // deliberate, so `codeOnly` strips comments and the assertion sees only the code.
    expect(go).not.toContain("window.history.pushState(null");
  });

  it("and so do in-console links", () => {
    const at = SHELL.indexOf("event.preventDefault();");
    expect(SHELL.slice(at, at + 400)).toContain("router.push");
  });

  it("panels are still told the URL changed", () => {
    // They restore their own selection by reading window.location.search on this signal, which
    // router.push has already updated. Same behaviour as Back/Forward.
    const go = SHELL.slice(SHELL.indexOf("const go = useCallback"),
                           SHELL.indexOf("Quick-action links"));
    expect(go).toContain('new PopStateEvent("popstate")');
  });

  it("and Back still returns to the previous tab", () => {
    const go = SHELL.slice(SHELL.indexOf("const go = useCallback"),
                           SHELL.indexOf("Quick-action links"));
    expect(go).toContain('url.searchParams.set("area", id)');
  });
});
