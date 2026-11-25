// @vitest-environment happy-dom
/**
 * The stop pill says why it is stopped, beside Stopped, not only in the title.
 * Not stopped says nothing. Blank or whitespace reason and who say nothing.
 * More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if a running pill still shows a reason, padding survives, who is
 * dropped or invented, a long reason is a span, Stopped or On disappears,
 * or StopPill stops calling stopPillSentence(stop.stopped, stop.by, stop.reason).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { StopPillFace, stopPillSentence } from "../app/agent-ops/agent-ops-home";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the staging probes have been failing since last night";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "bad release";
const WHO = "person:vee";
const ON_TITLE = "Agents may act within their levels. Open Autonomy & safety.";

type Stop = { stopped: boolean; by: string | null; reason: string };

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(stop: Stop): Promise<HTMLElement> {
  await act(async () => { root?.unmount(); });
  host?.remove();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<StopPillFace stop={stop} sentence={stopPillSentence(stop.stopped, stop.by, stop.reason)} />);
  });
  return host;
}

function pill(el: HTMLElement): HTMLElement {
  const node = el.querySelector<HTMLElement>("[data-testid='aa-stop-pill']");
  expect(node).not.toBeNull();
  return node as HTMLElement;
}

function why(el: HTMLElement): HTMLElement | null {
  return el.querySelector<HTMLElement>("[data-testid='aa-stop-why']");
}

describe("stopPillSentence", () => {
  it("is the trimmed reason and, when set, who stopped it, or nothing", () => {
    expect(stopPillSentence(true, `  ${WHO}  `, `  ${SHORT}  `)).toBe(`${SHORT}, ${WHO}`);
    expect(stopPillSentence(true, `  ${WHO}  `, `  ${LONG}  `)).toBe(`${LONG}, ${WHO}`);
    expect(stopPillSentence(true, `  ${WHO}  `, `  ${SIX}  `)).toBe(`${SIX}, ${WHO}`);
    expect(stopPillSentence(true, `  ${WHO}  `, `  ${SEVEN}  `)).toBe(`${SEVEN}, ${WHO}`);
    expect(stopPillSentence(true, null, `  ${SHORT}  `)).toBe(SHORT);
    expect(stopPillSentence(true, undefined, LONG)).toBe(LONG);
    expect(stopPillSentence(true, "", SIX)).toBe(SIX);
    expect(stopPillSentence(true, " \n\t ", SEVEN)).toBe(SEVEN);
    expect(stopPillSentence(true, `  ${WHO}  `, "")).toBe(WHO);
    expect(stopPillSentence(true, `  ${WHO}  `, " \n\t ")).toBe(WHO);
    expect(stopPillSentence(true, null, null)).toBeNull();
    expect(stopPillSentence(true, undefined, undefined)).toBeNull();
    expect(stopPillSentence(true, "", "")).toBeNull();
    expect(stopPillSentence(true, " \n\t ", " \n\t ")).toBeNull();
    expect(stopPillSentence(false, WHO, LONG)).toBeNull();
    expect(stopPillSentence(false, WHO, "")).toBeNull();
    expect(stopPillSentence(false, "", "")).toBeNull();
    expect(stopPillSentence(false, null, null)).toBeNull();
  });
});

describe("the stop pill says why it is stopped", () => {
  it("shows more than six words as a paragraph and six or fewer as a span, beside Stopped", async () => {
    const long = pill(await show({ stopped: true, by: null, reason: `  ${LONG}  ` }));
    const longWhy = why(long);
    expect(long.textContent).toContain("Stopped");
    expect(long.textContent).not.toContain("On");
    expect(long.getAttribute("title")).toBe(`Stopped,   ${LONG}  . Open Autonomy & safety.`);
    expect(long.getAttribute("data-tone")).toBe("red");
    expect(longWhy?.tagName).toBe("P");
    expect(longWhy?.textContent).toBe(LONG);
    expect(longWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);

    const seven = pill(await show({ stopped: true, by: " \n\t ", reason: SEVEN }));
    const sevenWhy = why(seven);
    expect(seven.textContent).toContain("Stopped");
    expect(seven.getAttribute("title")).toBe(`Stopped by  \n\t , ${SEVEN}. Open Autonomy & safety.`);
    expect(sevenWhy?.tagName).toBe("P");
    expect(sevenWhy?.textContent).toBe(SEVEN);
    expect(sevenWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);

    const six = pill(await show({ stopped: true, by: null, reason: SIX }));
    const sixWhy = why(six);
    expect(six.textContent).toContain("Stopped");
    expect(six.getAttribute("title")).toBe(`Stopped, ${SIX}. Open Autonomy & safety.`);
    expect(sixWhy?.tagName).toBe("SPAN");
    expect(sixWhy?.textContent).toBe(SIX);
    expect(sixWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    const short = pill(await show({ stopped: true, by: "", reason: SHORT }));
    const shortWhy = why(short);
    expect(short.textContent).toContain("Stopped");
    expect(short.getAttribute("title")).toBe(`Stopped, ${SHORT}. Open Autonomy & safety.`);
    expect(shortWhy?.tagName).toBe("SPAN");
    expect(shortWhy?.textContent).toBe(SHORT);
    expect(shortWhy?.hasAttribute("data-user-text")).toBe(true);
  });

  it("shows who stopped it with the reason, and who alone when the reason is blank", async () => {
    const both = pill(await show({ stopped: true, by: `  ${WHO}  `, reason: `  ${SHORT}  ` }));
    const bothWhy = why(both);
    expect(both.textContent).toContain("Stopped");
    expect(both.getAttribute("title")).toBe(`Stopped by   ${WHO}  ,   ${SHORT}  . Open Autonomy & safety.`);
    expect(bothWhy?.tagName).toBe("SPAN");
    expect(bothWhy?.textContent).toBe(`${SHORT}, ${WHO}`);
    expect(bothWhy?.hasAttribute("data-user-text")).toBe(true);
    expect(bothWhy?.textContent?.split(/\s+/).filter(Boolean)).toEqual(["bad", "release,", "person:vee"]);

    const pushed = pill(await show({ stopped: true, by: "extra", reason: SIX }));
    const pushedWhy = why(pushed);
    expect(pushed.getAttribute("title")).toBe(`Stopped by extra, ${SIX}. Open Autonomy & safety.`);
    expect(pushedWhy?.textContent).toBe(`${SIX}, extra`);
    expect(pushedWhy?.tagName).toBe("P");
    expect(`${SIX}, extra`.split(/\s+/).filter(Boolean).length).toBe(7);

    const who = pill(await show({ stopped: true, by: `  ${WHO}  `, reason: " \n\t " }));
    const whoWhy = why(who);
    expect(who.textContent).toContain("Stopped");
    expect(who.getAttribute("title")).toBe(`Stopped by   ${WHO}  ,  \n\t . Open Autonomy & safety.`);
    expect(whoWhy?.tagName).toBe("SPAN");
    expect(whoWhy?.textContent).toBe(WHO);
    expect(whoWhy?.textContent).not.toContain("by ");
  });

  it("says On and nothing else when agents are not stopped", async () => {
    const on = pill(await show({ stopped: false, by: WHO, reason: LONG }));
    expect(on.textContent).toBe("On");
    expect(on.getAttribute("title")).toBe(ON_TITLE);
    expect(on.getAttribute("data-tone")).toBe("green");
    expect(on.getAttribute("href")).toBe("/?area=budget&tab=autonomy");
    expect(why(on)).toBeNull();
  });

  it("keeps Stopped and the title when the reason and who are blank", async () => {
    const blank = pill(await show({ stopped: true, by: null, reason: "" }));
    expect(blank.textContent).toBe("Stopped");
    expect(blank.getAttribute("title")).toBe("Stopped. Open Autonomy & safety.");
    expect(blank.getAttribute("href")).toBe("/?area=budget&tab=autonomy");
    expect(why(blank)).toBeNull();

    const space = pill(await show({ stopped: true, by: " \n\t ", reason: " \n\t " }));
    expect(space.textContent).toBe("Stopped");
    expect(space.getAttribute("title")).toBe("Stopped by  \n\t ,  \n\t . Open Autonomy & safety.");
    expect(why(space)).toBeNull();
    expect(space.textContent).not.toContain("by");
  });

  it("calls stopPillSentence(stop.stopped, stop.by, stop.reason) from the stop pill", () => {
    const src = codeOnly(join(__dirname, "../app/agent-ops/agent-ops-home.tsx"));
    const start = src.indexOf("function StopPill(");
    expect(start).toBeGreaterThan(-1);
    const body = src.slice(start, src.indexOf("const LOADER_WORDS", start));
    expect(body).toContain("stopPillSentence(stop.stopped, stop.by, stop.reason)");
    expect(body).toContain("<StopPillFace");
    expect(body).toContain("sentence={stopPillSentence(stop.stopped, stop.by, stop.reason)}");
    expect(src).toContain('stop.stopped ? "Stopped" : "On"');
    expect(src).toContain("Open Autonomy & safety.");
  });
});
