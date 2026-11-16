/**
 * The preflight line says the detail, beside the label, not only on its title.
 * A blank detail falls back to the remedy. Blank, or the same as the label,
 * says nothing. More than six words is a paragraph. Six or fewer is a span.
 *
 * Breaks if the detail stays title-only, the label is replaced, a blank line
 * still renders, the remedy wins over a real detail, a same-as-label string
 * is repeated, a long line is a span, or the title changes.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

type SentenceFn = (
  detail: string | null | undefined,
  remedy: string | null | undefined,
  label: string | null | undefined,
) => string | null;
type WhyView = (props: { id: string | number; sentence: string | null }) => React.ReactNode;

const mod = await import("../app/infra/deploy-drawer") as {
  preflightDetailSentence?: SentenceFn;
  View?: WhyView;
};
const preflightDetailSentence = mod.preflightDetailSentence;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DETAIL = "port 443 is closed";
const REMEDY = "open 443 on the host";
const LABEL = "HTTPS";
const LONG = "this check failed because the host port is still closed";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SPACED = "one  two  three  four  five  six";
const SHORT = "port closed";

let root: Root | null = null;
let host: HTMLElement | null = null;

function unmountShown(): void {
  act(() => { root?.unmount(); });
  if (host) host.remove();
  root = null;
  host = null;
}

afterEach(() => {
  unmountShown();
  document.body.innerHTML = "";
});

async function show(id: string, sentence: string | null): Promise<HTMLElement> {
  unmountShown();
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
  return el.querySelector(`[data-testid='inf-deploy-preflight-why-${id}']`);
}

describe("preflightDetailSentence", () => {
  it("is a function that prefers a trimmed detail over the remedy", () => {
    expect(typeof preflightDetailSentence).toBe("function");
    expect(preflightDetailSentence!(`  ${DETAIL}  `, REMEDY, LABEL)).toBe(DETAIL);
    expect(preflightDetailSentence!(DETAIL, `  ${REMEDY}  `, LABEL)).toBe(DETAIL);
    expect(preflightDetailSentence!(DETAIL, REMEDY, `  ${LABEL}  `)).toBe(DETAIL);
  });

  it("falls back to the trimmed remedy when the detail is blank", () => {
    expect(preflightDetailSentence!(null, `  ${REMEDY}  `, LABEL)).toBe(REMEDY);
    expect(preflightDetailSentence!(undefined, REMEDY, LABEL)).toBe(REMEDY);
    expect(preflightDetailSentence!("", `  ${REMEDY}  `, LABEL)).toBe(REMEDY);
    expect(preflightDetailSentence!(" \n\t ", REMEDY, LABEL)).toBe(REMEDY);
  });

  it("says nothing when the chosen text is blank or equals the label", () => {
    expect(preflightDetailSentence!(null, null, LABEL)).toBeNull();
    expect(preflightDetailSentence!(undefined, undefined, LABEL)).toBeNull();
    expect(preflightDetailSentence!("", "", LABEL)).toBeNull();
    expect(preflightDetailSentence!(" \n\t ", "  ", LABEL)).toBeNull();
    expect(preflightDetailSentence!(null, null, null)).toBeNull();
    expect(preflightDetailSentence!(LABEL, REMEDY, LABEL)).toBeNull();
    expect(preflightDetailSentence!(`  ${LABEL}  `, REMEDY, ` ${LABEL} `)).toBeNull();
    expect(preflightDetailSentence!(null, LABEL, `  ${LABEL}  `)).toBeNull();
    expect(preflightDetailSentence!("", `  ${LABEL}  `, LABEL)).toBeNull();
    expect(preflightDetailSentence!(" \n\t ", LABEL, LABEL)).toBeNull();
  });
});

describe("the preflight line says the detail", () => {
  it("shows more than six words as a paragraph and six or fewer as a span", async () => {
    const long = why(await show("host", LONG), "host");
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe(LONG);
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    const seven = why(await show("dns", SEVEN), "dns");
    expect(seven?.tagName).toBe("P");
    expect(seven?.textContent).toBe(SEVEN);
    expect(seven?.hasAttribute("data-user-text")).toBe(true);

    const spaced = why(await show("disk", SPACED), "disk");
    expect(spaced?.tagName).toBe("SPAN");
    expect(spaced?.textContent).toBe(SPACED);
    expect(spaced?.hasAttribute("data-user-text")).toBe(true);

    const six = why(await show("tls", SIX), "tls");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe(SIX);
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    const short = why(await show("port", SHORT), "port");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe(SHORT);
    expect(short?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when there is no sentence", async () => {
    const none = await show("host", null);
    expect(why(none, "host")).toBeNull();
    expect(none.textContent).toBe("");
  });

  it("calls preflightDetailSentence beside the label and keeps the title", () => {
    const src = codeOnly(join(__dirname, "../app/infra/deploy-drawer.tsx"));
    const start = src.indexOf('testId="inf-deploy-preflight"');
    expect(start).toBeGreaterThan(-1);
    const end = src.indexOf('testId="inf-deploy-log"', start);
    expect(end).toBeGreaterThan(start);
    const row = src.slice(start, end);
    const labelAt = row.indexOf("{f.label}");
    const callAt = row.indexOf("preflightDetailSentence(f.detail, f.remedy, f.label)");
    expect(labelAt).toBeGreaterThan(-1);
    expect(callAt).toBeGreaterThan(labelAt);
    expect(row).toContain('title={f.detail ?? f.remedy ?? undefined}>{f.label}</span>');
    expect(row).toContain("<View");
    expect(row).toContain("id={f.kind || i}");
    expect(src).toContain("data-testid={`inf-deploy-preflight-why-${");
  });
});
