// @vitest-environment happy-dom
/**
 * The preview says its error, beside Stopped or Failed, not only in the title.
 * A blank error is nothing. More than six words is a paragraph.
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

const { PreviewErrorLine, previewErrorSentence } = await import("../app/factory/outputs");

const SHORT = "port in use";
const SIX = "one two three four five six";
const LONG = "one two three four five six seven";

describe("previewErrorSentence", () => {
  it("returns the trimmed error, and nothing when the error is blank", () => {
    expect(previewErrorSentence("  port in use  ")).toBe("port in use");
    expect(previewErrorSentence(SHORT)).toBe("port in use");
    expect(previewErrorSentence(SIX)).toBe("one two three four five six");
    expect(previewErrorSentence(LONG)).toBe("one two three four five six seven");
    expect(previewErrorSentence(null)).toBeNull();
    expect(previewErrorSentence(undefined)).toBeNull();
    expect(previewErrorSentence("")).toBeNull();
    expect(previewErrorSentence(" \n\t ")).toBeNull();
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

async function show(status: string, error: string): Promise<void> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<PreviewErrorLine status={status} error={error} sentence={previewErrorSentence(error)} />);
  });
}

describe("the preview says its error", () => {
  it("shows the trimmed error beside Stopped and beside Failed", async () => {
    await show("stopped", "  port in use  ");
    const stopped = document.querySelector("[data-testid=fx-preview-stopped]");
    expect(stopped?.textContent).toBe("Stopped");
    expect(stopped?.getAttribute("title")).toBe("  port in use  ");
    const short = document.querySelector("[data-testid=fx-preview-error]");
    expect(short?.tagName).toBe("SPAN");
    expect(short?.textContent).toBe("port in use");
    expect(short?.hasAttribute("data-user-text")).toBe(true);
    expect(short?.parentElement).not.toBe(stopped);

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show("expired", SHORT);
    expect(document.querySelector("[data-testid=fx-preview-stopped]")?.textContent).toBe("Stopped");
    expect(document.querySelector("[data-testid=fx-preview-error]")?.tagName).toBe("SPAN");
    expect(document.querySelector("[data-testid=fx-preview-error]")?.textContent).toBe("port in use");

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show("failed", SIX);
    const failed = document.querySelector(".fx-err");
    expect(failed?.textContent).toBe("Failed");
    expect(failed?.getAttribute("title")).toBe(SIX);
    expect(document.querySelector("[data-testid=fx-preview-stopped]")).toBeNull();
    const six = document.querySelector("[data-testid=fx-preview-error]");
    expect(six?.tagName).toBe("SPAN");
    expect(six?.textContent).toBe("one two three four five six");
    expect(six?.hasAttribute("data-user-text")).toBe(true);

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show("failed", `  ${LONG}  `);
    expect(document.querySelector(".fx-err")?.textContent).toBe("Failed");
    expect(document.querySelector(".fx-err")?.getAttribute("title")).toBe(`  ${LONG}  `);
    const long = document.querySelector("[data-testid=fx-preview-error]");
    expect(long?.tagName).toBe("P");
    expect(long?.textContent).toBe("one two three four five six seven");
    expect(long?.hasAttribute("data-user-text")).toBe(true);

    act(() => root?.unmount());
    host?.remove();
    document.body.innerHTML = "";

    await show("stopped", "   ");
    expect(document.querySelector("[data-testid=fx-preview-stopped]")?.textContent).toBe("Stopped");
    expect(document.querySelector("[data-testid=fx-preview-error]")).toBeNull();
  });

  it("calls previewErrorSentence(p.error) in the stopped and failed branch", () => {
    const src = codeOnly(join(__dirname, "../app/factory/outputs.tsx"));
    const start = src.indexOf("p.error && !running");
    expect(start).toBeGreaterThan(-1);
    const end = src.indexOf("{err}", start);
    expect(end).toBeGreaterThan(start);
    const branch = src.slice(start, end);
    expect(branch).toContain("previewErrorSentence(p.error)");
    expect(branch).toContain("<PreviewErrorLine");
    expect(branch).toContain("!running");
  });
});
