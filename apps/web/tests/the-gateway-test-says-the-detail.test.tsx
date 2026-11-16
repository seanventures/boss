/**
 * The gateway test says the detail, beside Works or Failed, not only on its title.
 * A blank detail says nothing. Works and Failed are already visible, so those words
 * say nothing again. More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the detail stays title-only, padding survives, a blank detail still
 * renders, Works or Failed is repeated, a long detail is a span, or the title changes.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type WhyFn = (detail: string | null | undefined) => string | null;
type WhyView = (props: { id: string; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/setup/gateways-row") as {
  gatewayTestDetailSentence?: WhyFn;
  View?: WhyView;
};
const gatewayTestDetailSentence = mod.gatewayTestDetailSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DIFFERENT = "the gateway timed out";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => { root?.unmount(); });
  if (host) host.remove();
  host = null;
  root = null;
  document.body.innerHTML = "";
});

async function show(id: string, sentence: string | null): Promise<HTMLElement> {
  act(() => { root?.unmount(); });
  if (host) host.remove();
  host = null;
  root = null;
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const View = mod.View!;
  await act(async () => {
    root!.render(<View id={id} sentence={sentence} />);
  });
  return host;
}

function why(el: HTMLElement, id: string): Element | null {
  return el.querySelector(`[data-testid='gateway-test-why-${id}']`);
}

describe("gatewayTestDetailSentence", () => {
  it("is the trimmed detail, or nothing when it is blank or already Works or Failed", () => {
    expect(typeof gatewayTestDetailSentence).toBe("function");
    expect(gatewayTestDetailSentence!(`  ${DIFFERENT}  `)).toBe(DIFFERENT);
    expect(gatewayTestDetailSentence!("Works now")).toBe("Works now");
    expect(gatewayTestDetailSentence!("Failed to answer")).toBe("Failed to answer");
    expect(gatewayTestDetailSentence!(SIX)).toBe(SIX);
    expect(gatewayTestDetailSentence!(SEVEN)).toBe(SEVEN);
    expect(gatewayTestDetailSentence!(SPACED)).toBe(SPACED);
    expect(gatewayTestDetailSentence!(null)).toBeNull();
    expect(gatewayTestDetailSentence!(undefined)).toBeNull();
    expect(gatewayTestDetailSentence!("")).toBeNull();
    expect(gatewayTestDetailSentence!(" \n\t ")).toBeNull();
    expect(gatewayTestDetailSentence!("Works")).toBeNull();
    expect(gatewayTestDetailSentence!("  Works  ")).toBeNull();
    expect(gatewayTestDetailSentence!("Failed")).toBeNull();
    expect(gatewayTestDetailSentence!("  Failed  ")).toBeNull();
  });
});

describe("the gateway test says the detail", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    expect(typeof mod.View).toBe("function");
    const seven = why(await show("g7", SEVEN), "g7");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven!.textContent!.split(/\s+/).filter(Boolean).length).toBe(7);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show("g6", SIX), "g6");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const spaced = why(await show("gs", SPACED), "gs");
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced!.textContent!.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);

    const different = why(await show("gd", DIFFERENT), "gd");
    expect(different?.tagName).toBe("SPAN");
    expect(different?.textContent).toBe(DIFFERENT);
    expect(different?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when the detail is blank, Works, or Failed", async () => {
    expect(typeof gatewayTestDetailSentence).toBe("function");
    const blank = await show("gb", gatewayTestDetailSentence!(" \n\t "));
    expect(why(blank, "gb")).toBeNull();
    expect(blank.textContent).toBe("");

    const works = await show("gw", gatewayTestDetailSentence!("  Works  "));
    expect(why(works, "gw")).toBeNull();
    expect(works.textContent).toBe("");

    const failed = await show("gf", gatewayTestDetailSentence!("Failed"));
    expect(why(failed, "gf")).toBeNull();
    expect(failed.textContent).toBe("");

    const none = await show("gn", null);
    expect(why(none, "gn")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls gatewayTestDetailSentence beside the test span and leaves the title", () => {
    const src = codeOnly(join(__dirname, "../app/setup/gateways-row.tsx"));
    const marker = "data-testid={`gateway-test-${g.id}`}";
    const at = src.indexOf(marker);
    expect(at).toBeGreaterThan(-1);
    const close = src.indexOf("</span>", at);
    expect(close).toBeGreaterThan(at);
    const span = src.slice(at, close);
    expect(span).toContain("title={g.last_test.detail}");
    expect(span).toContain('{" · "}');
    expect(span).toContain('g.last_test.ok ? "Works" : "Failed"');
    expect(span).not.toContain("gatewayTestDetailSentence");
    expect(span).not.toContain("<View");
    const after = src.slice(close, close + 300);
    expect(after).toContain("gatewayTestDetailSentence(g.last_test.detail)");
    expect(after).toContain("<View id={g.id} sentence={gatewayTestDetailSentence(g.last_test.detail)}");
  });
});
