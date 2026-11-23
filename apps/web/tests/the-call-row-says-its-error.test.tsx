/**
 * A failed call shows ✗ and keeps the error only on that mark's title.
 * The row says the error beside it. Nothing when the call succeeded, or the error is blank.
 * More than six words is a paragraph.
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { codeOnly } from "./_helpers/source";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { CallErrorLine, callErrorSentence } = await import("../app/observability/group-drawer");

const SHORT = "rate limited";
const SIX = "one two three four five six";
const LONG = "one two three four five six seven";

describe("callErrorSentence", () => {
  it("returns the trimmed error, and nothing when the call succeeded or the error is blank", () => {
    expect(callErrorSentence(false, "  rate limited  ")).toBe("rate limited");
    expect(callErrorSentence(false, SHORT)).toBe("rate limited");
    expect(callErrorSentence(false, SIX)).toBe("one two three four five six");
    expect(callErrorSentence(false, `  ${LONG}  `)).toBe("one two three four five six seven");
    expect(callErrorSentence(false, null)).toBeNull();
    expect(callErrorSentence(false, undefined)).toBeNull();
    expect(callErrorSentence(false, "")).toBeNull();
    expect(callErrorSentence(false, " \n\t ")).toBeNull();
    expect(callErrorSentence(true, "  rate limited  ")).toBeNull();
    expect(callErrorSentence(true, SHORT)).toBeNull();
    expect(callErrorSentence(true, "")).toBeNull();
    expect(callErrorSentence(true, " \n\t ")).toBeNull();
    expect(callErrorSentence(true, null)).toBeNull();
  });
});

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

async function show(ok: boolean, error: string | null): Promise<void> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<CallErrorLine sentence={callErrorSentence(ok, error)} />);
  });
}

describe("the call row says its error", () => {
  it("shows the trimmed error beside the mark, as a span up to six words and a paragraph after", async () => {
    await show(false, "  rate limited  ");
    const short = document.querySelector("[data-testid=obs-call-error]");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe("rate limited");
    expect(short?.hasAttribute("data-user-text")).toBe(true);

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show(false, SIX);
    const six = document.querySelector("[data-testid=obs-call-error]");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe("one two three four five six");
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show(false, `  ${LONG}  `);
    const long = document.querySelector("[data-testid=obs-call-error]");
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe("one two three four five six seven");
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show(false, "   ");
    expect(document.querySelector("[data-testid=obs-call-error]")).toBeNull();

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show(false, "");
    expect(document.querySelector("[data-testid=obs-call-error]")).toBeNull();

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show(false, null);
    expect(document.querySelector("[data-testid=obs-call-error]")).toBeNull();

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show(true, "  rate limited  ");
    expect(document.querySelector("[data-testid=obs-call-error]")).toBeNull();
  });

  it("calls callErrorSentence(c.ok, c.error) beside the existing ok and ✗ title", () => {
    const src = codeOnly(join(__dirname, "../app/observability/group-drawer.tsx"));
    const start = src.indexOf('data-testid="obs-call-row"');
    expect(start).toBeGreaterThan(-1);
    const end = src.indexOf("obs-calls-older", start);
    expect(end).toBeGreaterThan(start);
    const row = src.slice(start, end);
    expect(row).toContain("callErrorSentence(c.ok, c.error)");
    expect(row).toContain("<CallErrorLine");
    expect(row).toContain('title={c.ok ? "OK" : c.error ?? "The call failed and BOSS recorded no error"}');
    expect(row).toContain('{c.ok ? "ok" : "✗"}');
  });
});
