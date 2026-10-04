/**
 * The first paint is one area; opening another fetches it once, behind a real loading state.
 *
 * Rendering every area on every load cost 8.3 MB and 6-9 seconds for the first paint, and
 * every auto-refresh repeated it. Rendering one area per request made every rail click a round
 * trip and a full remount. lib/area-loading.ts takes neither: a browser load renders the
 * requested area alone, and an area opened for the first time joins a session set and is
 * rendered from then on, so a second click is the pure CSS reveal again.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  areasToRender,
  cookieWith,
  isFullDocumentRequest,
  loadedAreasCookie,
  loadedAreasCookieValue,
  parseLoadedAreas,
} from "../lib/area-loading";
import { codeOnly } from "./_helpers/source";

const headers = (h: Record<string, string>) => (name: string) => h[name] ?? null;

describe("which areas a render includes", () => {
  it("a browser load renders only the requested area, whatever the session opened", () => {
    expect([...areasToRender({ requested: "decisions", cookie: "overview,workforce",
                                fullDocument: true })]).toEqual(["decisions"]);
  });

  it("a refresh keeps every area the session opened, so none on screen is unmounted", () => {
    const areas = areasToRender({ requested: "decisions", cookie: "overview,workforce",
                                  fullDocument: false });
    expect([...areas].sort()).toEqual(["decisions", "overview", "workforce"]);
  });

  it("a router refresh and a server action both count as not a browser load", () => {
    // What a browser really sends for a page load and for the router's refresh fetch.
    expect(isFullDocumentRequest(headers({ "sec-fetch-dest": "document",
                                           accept: "text/html,application/xhtml+xml" }))).toBe(true);
    expect(isFullDocumentRequest(headers({ "sec-fetch-dest": "empty", accept: "*/*" }))).toBe(false);
    // The router's own headers never reach page code: Next.js 15 strips RSC and
    // Next-Router-State-Tree from headers(). Judging by them made EVERY request a "browser
    // load", so each refresh dropped the areas on screen and every switch refetched (boss-1).
    expect(isFullDocumentRequest(headers({ accept: "*/*" }))).toBe(false);
    expect(isFullDocumentRequest(headers({ accept: "text/html" }))).toBe(true);
    expect(isFullDocumentRequest(headers({ rsc: "1" }))).toBe(false);
    // A server action re-renders the page the operator has open — dropping its areas would
    // unmount them under the button that was just pressed.
    expect(isFullDocumentRequest(headers({ "next-action": "abc" }))).toBe(false);
  });

  it("a forged or malformed cookie cannot ask for anything but area ids, and is bounded", () => {
    expect(parseLoadedAreas("overview,<script>,../etc,DROP TABLE")).toEqual(["overview"]);
    expect(parseLoadedAreas(Array.from({ length: 500 }, (_, i) => `a${i}`).join(","))
      .length).toBeLessThanOrEqual(64);
    expect(areasToRender({ requested: "<bad>", cookie: "", fullDocument: true }))
      .toEqual(new Set(["overview"]));
  });

  it("the cookie round-trips and only grows", () => {
    const one = cookieWith("", "workforce");
    const two = cookieWith(one, "decisions");
    expect(parseLoadedAreas(two)).toEqual(["workforce", "decisions"]);
    expect(cookieWith(two, "workforce")).toBe(two);
    const header = loadedAreasCookie(two);
    expect(header).toContain("path=/");
    expect(header).toContain("SameSite=Lax");
    expect(loadedAreasCookieValue(`a=1; ${header.split(";")[0]}; b=2`))
      .toBe(encodeURIComponent(two));
  });
});

describe("both pages and the shell use it", () => {
  // The Community tree has one page: the private repo's page-community.tsx IS this page.tsx
  // (the export's page_export_rewrite), so the console-a stream's two-page loop reads one here.
  for (const page of ["app/page.tsx"]) {
    it(`${page} gates rendering and fetching on the same set`, () => {
      const src = codeOnly(page);
      expect(src).toContain("const loadedAreas = areasToRender({");
      expect(src).toContain("const shows = (area: string) => loadedAreas.has(area);");
      expect(src).toContain(
        "const needs = (...areas: string[]) => areas.some((area) => loadedAreas.has(area));");
      expect(src).toContain("loadedAreas={[...loadedAreas]}");
      expect(src).not.toContain("const shows = (_area: string) => true;");
    });
  }

  it("the shell fetches an unopened area once and says so while it does", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    const ensure = shell.slice(shell.indexOf("const ensureLoaded = useCallback"),
                               shell.indexOf("const activeLoading"));
    expect(ensure).toContain("loaded.includes(id)");
    expect(ensure).toContain("document.cookie = loadedAreasCookie(");
    // Through the console's one refresh coordinator (lib/console-refresh.ts), never alongside
    // a refresh already in flight.
    expect(ensure).toContain('refreshCoordinator.request("now")');
    expect(shell).toContain('data-testid="area-loading"');
    expect(shell).toContain("Loading {activeDef.label}…");
    // And a load that never lands says so, with a way out.
    expect(shell).toContain("This is taking longer than usual");
  });

  it("a rail click on an opened area still touches the server zero times", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    const at = shell.indexOf("const go = useCallback");
    const go = shell.slice(at, shell.indexOf("}, []);", at));
    expect(go).toContain("window.history.pushState(null");
    expect(go).not.toContain("router.push");
    expect(go).not.toContain("router.refresh");
  });
});

it("the loader's styles ship with the console", () => {
  expect(readFileSync("app/layout.tsx", "utf8")).toContain('import "./command-center.css";');
  expect(readFileSync("app/command-center.css", "utf8")).toContain(".area-loading");
});
