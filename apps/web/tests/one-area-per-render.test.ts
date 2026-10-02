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
    //
    // AND THEN LAZY, WITHOUT GIVING THE CLICK BACK (lib/area-loading.ts). A browser load renders
    // the requested area alone; an area opened for the first time joins a session set and is
    // fetched once, with a loading state; after that a click is the pure reveal again. Still
    // one seam — the set below is the only thing either gate reads.
    expect(PAGE).toContain("const shows = (area: string) => loadedAreas.has(area);");
    expect(PAGE).toContain("const loadedAreas = areasToRender({");
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

describe("switching areas does not reach the server at all", () => {
  it("the sidebar rewrites the URL rather than navigating", () => {
    // REVERSED, AND THIS IS THE WHOLE REASON THE PAGE RENDERS EVERY AREA AGAIN.
    //
    // `router.push` is a soft navigation: the server re-renders this tree and streams it back.
    // That was correct while the page rendered one area per request, because the section being
    // opened genuinely did not exist in the browser yet. With every area in the DOM it is pure
    // cost — a 6.3 MB, 6-9 second round trip to fetch a screen already on screen. That is the
    // "several seconds per menu click" this whole file was supposed to have fixed.
    //
    // pushState keeps the URL honest and Back working and asks the server for nothing.
    const go = SHELL.slice(SHELL.indexOf("const go = useCallback"),
                           SHELL.indexOf("Quick-action links"));
    expect(go).toContain("window.history.pushState(null");
    expect(go).not.toContain("router.push");
  });

  it("and so do in-console links", () => {
    // Same reveal, same reason. A quick-action link that pushed would make the Command Center
    // the slowest route into every area it advertises.
    const at = SHELL.indexOf("event.preventDefault();");
    const handler = SHELL.slice(at, at + 500);
    expect(handler).toContain("window.history.pushState(null");
    expect(handler).not.toContain("router.push");
  });

  it("panels are told the URL changed, by whoever changed it", () => {
    // pushState fires no popstate of its own, so the code that calls it announces. This is safe
    // here in a way it was not with router.push: pushState is SYNCHRONOUS, so by the time the
    // event is dispatched `window.location.search` already holds the new area. The ~30s-a-click
    // bug was announcing before an asynchronous navigation had committed.
    const at = SHELL.indexOf("const go = useCallback");
    const go = SHELL.slice(at, SHELL.indexOf("}, []);", at));
    expect(go).toContain('new PopStateEvent("popstate")');
    const pushAt = go.indexOf("window.history.pushState(null");
    expect(pushAt).toBeGreaterThan(-1);
    expect(go.indexOf('new PopStateEvent("popstate")')).toBeGreaterThan(pushAt);
    // Back/Forward still needs the effect, because nothing announces those.
    expect(SHELL).toContain("const announcedSearch = useRef<string | null>(null);");
  });

  it("and Back still returns to the previous tab", () => {
    const at = SHELL.indexOf("const go = useCallback");
    const go = SHELL.slice(at, SHELL.indexOf("}, []);", at));
    expect(go).toContain('url.searchParams.set("area", id)');
  });
});


describe("the announcement waits for the url to be true", () => {
  it("nothing announces popstate before the url is actually true", () => {
    // This cost ~30 seconds per click when the rail used `router.push`: that is ASYNCHRONOUS,
    // so announcing on the next line told ELEVEN listeners the url had changed while
    // window.location still held the old area — they re-read a stale url and refetched, the
    // navigation committed, the area remounted, and they all fetched again.
    //
    // pushState is synchronous, so announcing immediately after it is correct: the url is
    // already new. What must never come back is an announcement racing an async navigation.
    // Sliced to the callback's own end, not to a comment — `codeOnly` strips comments, so a
    // comment landmark returns -1 and the slice silently becomes most of the file.
    const at = SHELL.indexOf("const go = useCallback");
    const go = SHELL.slice(at, SHELL.indexOf("}, []);", at));
    expect(go).not.toContain("router.push");
    const push = go.indexOf("window.history.pushState(null");
    const announce = go.indexOf('new PopStateEvent("popstate")');
    expect(push).toBeGreaterThan(-1);
    expect(announce).toBeGreaterThan(push);
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
