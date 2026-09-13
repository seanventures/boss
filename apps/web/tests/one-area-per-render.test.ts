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
  it("the gate is one seam, so the trade can be flipped in two lines", () => {
    // THIS DECISION WAS REVERSED, AND THE TEST SAYS SO RATHER THAN BEING DELETED.
    //
    // Rendering one area per request achieved what this file describes — 8.3 MB down to
    // 25-235 KB — and cost something the measurements here never captured: every menu click
    // became a server round trip AND a full React teardown, ~50 components unmounted and ~50
    // mounted. The operator reported that repeatedly, in escalating terms, and was right. A
    // click used to do no work at all, because every area was already in the DOM and
    // `main[data-active=X]` revealed one with CSS.
    //
    // So `shows` returns true and the sections below all render again. What is still worth
    // protecting is that this stayed ONE seam: the gate every section passes through, so the
    // trade is two lines to flip rather than a rewrite to undo.
    expect(PAGE).toContain("const shows = (_area: string) => true;");
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

  it("panels are still told the URL changed — once it is actually true", () => {
    // This used to assert the announcement came from `go`, on the line after router.push, and
    // that is precisely what cost ~30 seconds a click: the url had not changed yet, so eleven
    // listeners re-read a stale one and refetched. The PROPERTY — panels get told — is
    // unchanged; where it is announced from is the fix.
    const at = SHELL.indexOf("const go = useCallback");
    const go = SHELL.slice(at, SHELL.indexOf("}, [router]);", at));
    expect(go).not.toContain("PopStateEvent");
    expect(SHELL).toContain("const announcedSearch = useRef<string | null>(null);");
    const effect = SHELL.slice(SHELL.indexOf("const announcedSearch"),
                               SHELL.indexOf("const announcedSearch") + 700);
    expect(effect).toContain('new PopStateEvent("popstate")');
  });

  it("and Back still returns to the previous tab", () => {
    const at = SHELL.indexOf("const go = useCallback");
    const go = SHELL.slice(at, SHELL.indexOf("}, [router]);", at));
    expect(go).toContain('url.searchParams.set("area", id)');
  });
});

describe("the announcement waits for the url to be true", () => {
  it("nothing announces popstate on the same line as router.push", () => {
    // This cost ~30 seconds per click. `router.push` is asynchronous, so announcing on the next
    // line tells ELEVEN listeners the url changed while window.location still holds the old
    // area: they all re-read a stale url and refetch, the navigation then commits, the area
    // remounts, and they all fetch again — a double herd per click, on the main thread.
    // Sliced to the callback's own end, not to a comment — `codeOnly` strips comments, so a
    // comment landmark returns -1 and the slice silently becomes most of the file.
    const at = SHELL.indexOf("const go = useCallback");
    const go = SHELL.slice(at, SHELL.indexOf("}, [router]);", at));
    expect(go).toContain("router.push");
    expect(go).not.toContain("PopStateEvent");
  });

  it("it is announced from the committed search params instead", () => {
    expect(SHELL).toContain("const announcedSearch = useRef<string | null>(null);");
    const at = SHELL.indexOf("const announcedSearch");
    const block = SHELL.slice(at, at + 700);
    expect(block).toContain('new PopStateEvent("popstate")');
    expect(block).toContain("}, [searchParams]);");
  });

  it("and the first render is not announced as a navigation", () => {
    // Announcing it would make every panel refetch on arrival — the thing the server render
    // just did for them.
    const at = SHELL.indexOf("const announcedSearch");
    expect(SHELL.slice(at, at + 700)).toContain("announcedSearch.current === null");
  });
});
