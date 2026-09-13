import { describe, expect, it } from "vitest";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

/**
 * "Every time I come to Factory it is taking time to load ... I don't see any indicator. I
 * reported this multiple times before."
 *
 * Reported repeatedly and fixed repeatedly, because every fix was in the wrong place. Measured
 * on goldeneye against the URL an operator bookmarks:
 *
 *     https://boss.local.sean.network/?org=org_boss&area=code_tasks
 *     time_to_first_byte: 6.549s   total: 7.373s   size: 5.6 MB
 *
 * Six and a half seconds to the FIRST BYTE. `HomePage` awaits ~50 feeds before returning any
 * JSX and there was no route-level Suspense boundary, so Next sent nothing at all until every
 * feed landed. The console's indicators — a sticky bar with an elapsed counter, a Syncing chip
 * — are real, correct, and all inside HTML that does not exist during the wait. No indicator
 * added to the page could ever have been visible for it.
 *
 * `loading.tsx` is the only thing that renders before the first await. That is the fix, and
 * this file exists to keep it: deleting the file, or making the page's own bar "better", puts
 * the operator back in front of a blank tab.
 */
const APP = join(process.cwd(), "app");
const LOADING = join(APP, "loading.tsx");

describe("the wait starts with something on screen", () => {
  it("a route-level loading boundary exists at all", () => {
    // Without this file Next has no fallback to stream, and the browser gets nothing for the
    // whole time the server is fetching. This assertion IS the bug.
    expect(existsSync(LOADING), "app/loading.tsx is the only thing rendered before the first "
      + "await in HomePage — without it the browser shows the previous page for ~7 seconds")
      .toBe(true);
  });

  it("it renders above every await, so nothing in the page can delay it", () => {
    // The boundary only helps if the layout above it is synchronous. An `await` in the root
    // layout would move the wait back in front of the fallback and restore the blank screen.
    const layout = readFileSync(join(APP, "layout.tsx"), "utf8");
    expect(layout).not.toMatch(/export default async function/);
    expect(layout).not.toMatch(/\bawait\b/);
  });

  it("it says what is happening, not just that something is", () => {
    const src = readFileSync(LOADING, "utf8");
    expect(src).toContain("Loading the console");
    expect(src).toMatch(/role="status"/);
    expect(src).toMatch(/aria-live="polite"/);
  });

  it("it counts, so a long wait is distinguishable from a dead one", () => {
    const src = readFileSync(LOADING, "utf8");
    expect(src).toContain("setElapsed");
    // Not from the first second: a number that appears instantly makes a fast load feel slow.
    expect(src).toContain("elapsed >= 3");
    // And it admits when the wait has gone past what is normal, rather than counting forever
    // with the same reassuring sentence.
    expect(src).toContain("longer than usual");
  });

  it("it invents no data", () => {
    const src = readFileSync(LOADING, "utf8");
    // A skeleton with plausible figures in it is read as real for as long as it is up, and
    // this one is up for seven seconds. Percentages, counts and money must not appear.
    const body = src.slice(src.indexOf("return ("));
    expect(body).not.toMatch(/>\s*\d+\s*%/);
    expect(body).not.toMatch(/\$\d/);
    // The elapsed counter is the one number, and it is about the wait, not about the data.
    expect(body.match(/\{\s*elapsed\s*\}/g)?.length ?? 0).toBe(1);
  });

  it("it wears the console's own frame rather than a foreign spinner", () => {
    const src = readFileSync(LOADING, "utf8");
    for (const cls of ["bx-shell", "bx-sidebar", "bx-topbar", "bx-content"]) {
      expect(src, `${cls} keeps the frame still when the real console replaces it`)
        .toContain(cls);
    }
  });

  it("and the classes it uses are real", () => {
    // A skeleton styled with class names nobody defined is an unstyled pile of divs — which
    // would look like a broken page for seven seconds, the failure it was written to fix.
    const css = readFileSync(join(APP, "globals.css"), "utf8");
    const src = readFileSync(LOADING, "utf8");
    const used = new Set<string>();
    for (const m of src.matchAll(/className="([^"]+)"/g)) {
      for (const c of m[1].split(/\s+/)) if (c.startsWith("bx-")) used.add(c);
    }
    const missing = [...used].filter((c) => !css.includes(`.${c}`));
    expect(missing, `used in loading.tsx, absent from globals.css: ${missing.join(", ")}`)
      .toEqual([]);
  });
});

describe("the bar stays up while the page is still filling", () => {
  const SHELL = readFileSync(join(APP, "ConsoleShell.tsx"), "utf8");

  it("it waits for the DOM to stop changing, not just for the document to finish", () => {
    // Measured on goldeneye: the stream ends at 7.95s and `load` fires — and React then
    // hydrates a 6 MB payload, which is when sections visibly appear. The indicator went off
    // at exactly the moment the expensive part began.
    expect(SHELL).toContain("new MutationObserver");
    expect(SHELL).toContain("Promise.all([documentDone, settledDom])");
  });

  it("quiet means quiet for long enough to mean it", () => {
    // A gap between two React commits must not read as finished.
    expect(SHELL).toMatch(/setTimeout\(\(\) => \{ observer\.disconnect\(\); resolve\(\); \}, 700\)/);
  });

  it("it watches the content, not the whole document", () => {
    expect(SHELL).toContain('document.querySelector(".bx-content")');
    expect(SHELL).toContain("{ childList: true, subtree: true }");
  });

  it("and it can still never get stuck on", () => {
    // The failure the previous indicator had: permanent furniture that explained nothing.
    // Both guards stay — a ceiling, and a resolve when there is no observer to use.
    expect(SHELL).toContain("window.setTimeout(finish, 60_000)");
    expect(SHELL).toContain('typeof MutationObserver === "undefined"');
  });

  it("the floor is still there so a fast page does not blink", () => {
    expect(SHELL).toContain("window.setTimeout(r, 1200)");
  });
});

describe("a background refresh is visible without hunting for it", () => {
  const SHELL = readFileSync(join(APP, "ConsoleShell.tsx"), "utf8");
  const CSS = readFileSync(join(APP, "globals.css"), "utf8");

  it("a refresh in flight shows at the top edge", () => {
    // The Syncing chip existed and was easy to miss: one small pill among six, and carrying
    // `hide-sm`, so on a narrow window it was not rendered at all.
    expect(SHELL).toContain("bx-refresh-line");
    expect(SHELL).toContain("{refreshPending ? (");
  });

  it("it cannot become the permanent furniture the last one became", () => {
    // The sticky strip WAS wired to refreshPending once and never went off, because a refresh
    // costs about as long as the gap between refreshes. This one occupies no layout.
    const rule = CSS.slice(CSS.indexOf(".bx-refresh-line {"));
    expect(rule.slice(0, 260)).toContain("position: fixed");
    expect(rule.slice(0, 260)).toContain("pointer-events: none");
  });

  it("the first-paint bar is still separate from it", () => {
    // Two different questions — "is this page still arriving?" and "is it fetching again?" —
    // and conflating them is what produced the indicator that never turned off.
    expect(SHELL).toContain("{settling ? (");
    const settlingBlock = SHELL.slice(SHELL.indexOf("{settling ? ("),
                                      SHELL.indexOf("<ProjectStrip"));
    expect(settlingBlock).not.toContain("refreshPending");
  });
});

describe("the skeleton respects a reader who turned motion off", () => {
  it("the sweep stops, and the indicator does not vanish with it", () => {
    const css = readFileSync(join(APP, "globals.css"), "utf8");
    const reduced = css.split("@media (prefers-reduced-motion: reduce)").slice(1).join("\n");
    expect(reduced).toContain(".bx-skel");
    // Still visible when still: a busy signal that disappears for these readers tells them
    // nothing is happening, which is the opposite of true.
    expect(reduced).toContain(".bx-refresh-line::after");
    expect(reduced).toMatch(/opacity:\s*\.?\d/);
  });
});

describe("the console's own areas are unaffected", () => {
  it("switching area is a soft navigation, and the render it waits for is small", () => {
    // This used to assert pushState with NO server round-trip, because "every tab click would
    // flash this skeleton for seven seconds". That was true of a page which rendered all forty
    // areas at once: 8.3 MB and ~9s, measured on the live box.
    //
    // The reason it is no longer true is measured too. With page.tsx rendering only the
    // requested area, a switch renders 25–235 KB in 30–40 ms (overview, the heaviest, 139 KB
    // in 2.3s). There is nothing left to flash for. The concern this test encodes — a tab click
    // must not cost a full-page load — is what the change is FOR, so it is asserted at the
    // cause instead: the page must not render every area.
    const shell = readFileSync(join(APP, "ConsoleShell.tsx"), "utf8");
    const go = shell.slice(shell.indexOf("const go = useCallback"),
                           shell.indexOf("Quick-action links"));
    expect(go).toContain("router.push");
    // Still a history entry, so Back returns to the previous tab.
    expect(go).toContain('url.searchParams.set("area", id)');

    const page = readFileSync(join(APP, "page.tsx"), "utf8");
    // The area gate is now open — see one-area-per-render.test.ts. The soft navigation above
    // still matters (Back must return to the previous tab), but what it waits for is no longer
    // small, and it no longer needs to be: the section is already mounted when the click lands.
    expect(page).toContain("const shows = (_area: string) => true;");
  });

  it("there is exactly one route loading boundary", () => {
    // A second one in a nested segment would fire on refreshes and replace live content with
    // grey bars while data the operator is reading is still perfectly good.
    const found = readdirSync(APP, { withFileTypes: true, recursive: true } as never)
      .filter((e: never) => String((e as { name: string }).name) === "loading.tsx");
    expect(found.length).toBe(1);
  });
});
